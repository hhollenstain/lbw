"""Sign in with the provider saved for this organization. No new person is created."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import zlib
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.x509 import load_der_x509_certificate

from agent_lbw.runtime.db import Database
from agent_lbw.runtime.store.auth import open_session_for_email
from agent_lbw.runtime.store.crypto import open_secret
from agent_lbw.runtime.store.tasks import tenant_dek

_TTL = 10 * 60
_SKEW = 60
_HTTPS = "https://"
_SAML_REDIRECT = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
_SAML_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
_pending: dict[str, tuple[float, str, str, str, str, str]] = {}


def begin_sso(db: Database, root: Path, *, redirect_uri: str) -> str:
    """The provider page for the organization on this install."""
    tenant_id, row = _provider(db)
    provider = str(row["provider"])
    if provider == "saml":
        return _begin_saml(db, root, tenant_id, row, redirect_uri)
    return _begin_oidc(tenant_id, row, redirect_uri)


def finish_sso(
    db: Database,
    root: Path,
    *,
    code: str = "",
    state: str = "",
    saml_response: str = "",
    redirect_uri: str = "",
) -> str:
    """Check the assertion and open a session for an email that already exists."""
    pending = _pending.pop(state, None)
    if pending is None or pending[0] < time.time():
        raise ValueError("email or password is wrong")
    _expiry, tenant_id, verifier, nonce, kind = pending[:5]
    if kind == "access":
        raise ValueError("email or password is wrong")
    row = _row(db, tenant_id)
    if row is None or row["provider"] == "password":
        raise ValueError("email or password is wrong")
    if kind == "saml":
        email = _saml_email(row, saml_response)
    else:
        email = _oidc_email(
            root,
            db,
            tenant_id,
            row,
            code=code,
            verifier=verifier,
            nonce=nonce,
            redirect_uri=redirect_uri,
        )
    return open_session_for_email(db, tenant_id=tenant_id, email=email)


def begin_access(
    db: Database,
    root: Path,
    *,
    redirect_uri: str,
    audience: str = "",
    endpoint_id: str = "",
) -> str:
    """The organization sign-in page, asking for an access token a gateway can check."""
    del root
    tenant_id, row = _provider(db)
    if str(row["provider"]) == "saml":
        raise ValueError("a gateway sign-in needs Entra or Auth0")
    return _begin_oidc(
        tenant_id,
        row,
        redirect_uri,
        kind="access",
        audience=audience.strip(),
        endpoint_id=endpoint_id,
    )


def finish_access(
    db: Database,
    root: Path,
    *,
    code: str,
    state: str,
    redirect_uri: str,
) -> dict[str, str | int]:
    """Exchange the code and return the access token. The token is not stored here."""
    pending = _pending.pop(state, None)
    if pending is None or pending[0] < time.time():
        raise ValueError("this sign-in could not be checked")
    if len(pending) < 7:
        raise ValueError("this sign-in could not be checked")
    _expiry, tenant_id, verifier, nonce, kind, audience, endpoint_id = pending
    if kind != "access":
        raise ValueError("this sign-in could not be checked")
    row = _row(db, tenant_id)
    if row is None or row["provider"] in {"password", "saml"}:
        raise ValueError("a gateway sign-in needs Entra or Auth0")
    payload = _exchange_code(
        root,
        db,
        tenant_id,
        row,
        code=code,
        verifier=verifier,
        redirect_uri=redirect_uri,
        audience=audience,
    )
    identity = str(payload.get("id_token") or "")
    if not identity:
        raise ValueError("this sign-in could not be checked")
    document = _discovery(row)
    email = _id_token_email(
        identity,
        issuer=_document_issuer(document, row),
        audience=str(row["client_id"]),
        nonce=nonce,
        jwks=_get_json(_https(str(document.get("jwks_uri") or ""))),
    )
    access = str(payload.get("access_token") or "")
    if not access:
        raise ValueError("the provider did not return an access token")
    refresh = str(payload.get("refresh_token") or "")
    try:
        expires_in = int(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    return {
        "email": email,
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": max(expires_in, 60),
        "endpoint_id": endpoint_id,
    }


def refresh_access(
    db: Database,
    root: Path,
    *,
    refresh_token: str,
    audience: str = "",
) -> dict[str, str | int]:
    """Ask the organization provider for a new access token."""
    tenant_id, row = _provider(db)
    if str(row["provider"]) == "saml":
        raise ValueError("a gateway sign-in needs Entra or Auth0")
    document = _discovery(row)
    token_url = _https(str(document.get("token_endpoint") or ""))
    secret = _secret(db, root, tenant_id, row)
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": row["client_id"],
        "client_secret": secret,
    }
    if audience.strip():
        form["audience"] = audience.strip()
    try:
        response = httpx.post(
            token_url,
            data=form,
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise ValueError("this sign-in could not be checked") from exc
    finally:
        del secret
    if response.status_code >= 400:
        raise ValueError("sign in to this gateway again")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("this sign-in could not be checked") from exc
    if not isinstance(payload, dict):
        raise ValueError("this sign-in could not be checked")
    access = str(payload.get("access_token") or "")
    if not access:
        raise ValueError("the provider did not return an access token")
    try:
        expires_in = int(payload.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    renewed = str(payload.get("refresh_token") or refresh_token)
    return {
        "access_token": access,
        "refresh_token": renewed,
        "expires_in": max(expires_in, 60),
    }


def _provider(db: Database) -> tuple[str, dict]:
    install = db.fetchone("SELECT tenant_id FROM install WHERE id = 1")
    if install is None or not install["tenant_id"]:
        raise ValueError("create the admin account first")
    tenant_id = str(install["tenant_id"])
    row = _row(db, tenant_id)
    if row is None or row["provider"] == "password":
        raise ValueError("this organization uses a password")
    if not str(row["client_secret_ciphertext"] or "") and row["provider"] != "saml":
        raise ValueError("this sign-in could not be checked")
    return tenant_id, row


def _row(db: Database, tenant_id: str) -> dict | None:
    return db.fetchone(
        """
        SELECT provider, issuer, metadata_url, client_id, client_secret_ciphertext
        FROM sign_in WHERE tenant_id = ?
        """,
        (tenant_id,),
    )


def _exchange_code(
    root: Path,
    db: Database,
    tenant_id: str,
    row: dict,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    audience: str,
) -> dict:
    if not code:
        raise ValueError("this sign-in could not be checked")
    document = _discovery(row)
    token_url = _https(str(document.get("token_endpoint") or ""))
    secret = _secret(db, root, tenant_id, row)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": row["client_id"],
        "client_secret": secret,
        "code_verifier": verifier,
    }
    if audience:
        form["audience"] = audience
    try:
        response = httpx.post(
            token_url,
            data=form,
            headers={"Accept": "application/json"},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise ValueError("this sign-in could not be checked") from exc
    finally:
        del secret
    if response.status_code >= 400:
        raise ValueError("this sign-in could not be checked")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("this sign-in could not be checked") from exc
    if not isinstance(payload, dict):
        raise ValueError("this sign-in could not be checked")
    return payload


def _secret(db: Database, root: Path, tenant_id: str, row: dict) -> str:
    sealed = str(row["client_secret_ciphertext"] or "")
    if not sealed:
        raise ValueError("this sign-in could not be checked")
    return open_secret(tenant_dek(db, root, tenant_id), sealed)


def _begin_oidc(
    tenant_id: str,
    row: dict,
    redirect_uri: str,
    *,
    kind: str = "oidc",
    audience: str = "",
    endpoint_id: str = "",
) -> str:
    document = _discovery(row)
    endpoint = _https(str(document.get("authorization_endpoint") or ""))
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(24)
    _pending[state] = (
        time.time() + _TTL,
        tenant_id,
        verifier,
        nonce,
        kind,
        audience,
        endpoint_id,
    )
    _drop_expired()
    scope = "openid email offline_access" if kind == "access" else "openid email"
    fields = {
        "client_id": row["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": _pkce(verifier),
        "code_challenge_method": "S256",
    }
    if audience:
        fields["audience"] = audience
    return f"{endpoint}?{urlencode(fields)}"


def _oidc_email(
    root: Path,
    db: Database,
    tenant_id: str,
    row: dict,
    *,
    code: str,
    verifier: str,
    nonce: str,
    redirect_uri: str,
) -> str:
    if not code:
        raise ValueError("email or password is wrong")
    document = _discovery(row)
    jwks_url = _https(str(document.get("jwks_uri") or ""))
    try:
        payload = _exchange_code(
            root,
            db,
            tenant_id,
            row,
            code=code,
            verifier=verifier,
            redirect_uri=redirect_uri,
            audience="",
        )
    except ValueError as exc:
        raise ValueError("email or password is wrong") from exc
    token = str(payload.get("id_token") or "") if isinstance(payload, dict) else ""
    if not token:
        raise ValueError("email or password is wrong")
    jwks = _get_json(jwks_url)
    issuer = _document_issuer(document, row)
    return _id_token_email(
        token,
        issuer=issuer,
        audience=str(row["client_id"]),
        nonce=nonce,
        jwks=jwks,
    )


def _document_issuer(document: dict, row: dict) -> str:
    found = str(document.get("issuer") or row["issuer"] or "")
    if found and found != str(row["issuer"]):
        raise ValueError("email or password is wrong")
    return str(row["issuer"])


def _discovery(row: dict) -> dict:
    metadata = str(row["metadata_url"] or "").strip()
    url = metadata or f"{str(row['issuer']).rstrip('/')}/.well-known/openid-configuration"
    document = _get_json(url)
    if not isinstance(document, dict):
        raise ValueError("this sign-in could not be checked")
    return document


def _get_json(url: str) -> dict:
    target = _https(url)
    try:
        response = httpx.get(target, timeout=20)
    except httpx.HTTPError as exc:
        raise ValueError("this sign-in could not be checked") from exc
    if response.status_code >= 400:
        raise ValueError("this sign-in could not be checked")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("this sign-in could not be checked") from exc
    if not isinstance(payload, dict):
        raise ValueError("this sign-in could not be checked")
    return payload


def _id_token_email(token: str, *, issuer: str, audience: str, nonce: str, jwks: dict) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("email or password is wrong")
    try:
        header = json.loads(_b64(parts[0]))
        payload = json.loads(_b64(parts[1]))
        signature = _b64(parts[2])
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("email or password is wrong") from exc
    if not isinstance(header, dict) or header.get("alg") != "RS256":
        raise ValueError("email or password is wrong")
    if not isinstance(payload, dict):
        raise ValueError("email or password is wrong")
    key = _jwk(jwks, str(header.get("kid") or ""))
    try:
        key.verify(
            signature,
            f"{parts[0]}.{parts[1]}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise ValueError("email or password is wrong") from exc
    if str(payload.get("iss") or "") != issuer:
        raise ValueError("email or password is wrong")
    aud = payload.get("aud")
    allowed = aud if isinstance(aud, list) else [aud]
    if audience not in {str(item) for item in allowed}:
        raise ValueError("email or password is wrong")
    try:
        expires = int(payload.get("exp") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("email or password is wrong") from exc
    if expires < int(time.time()) - _SKEW:
        raise ValueError("email or password is wrong")
    if str(payload.get("nonce") or "") != nonce:
        raise ValueError("email or password is wrong")
    if payload.get("email_verified") is False:
        raise ValueError("email or password is wrong")
    email = str(payload.get("email") or "")
    if not email:
        raise ValueError("email or password is wrong")
    return email


def _jwk(jwks: dict, kid: str):
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list):
        raise ValueError("email or password is wrong")
    chosen = None
    for item in keys:
        if not isinstance(item, dict):
            continue
        if item.get("kty") != "RSA":
            continue
        if kid and str(item.get("kid") or "") != kid:
            continue
        chosen = item
        break
    if chosen is None or "n" not in chosen or "e" not in chosen:
        raise ValueError("email or password is wrong")
    try:
        numbers = RSAPublicNumbers(
            int.from_bytes(_b64(str(chosen["e"])), "big"),
            int.from_bytes(_b64(str(chosen["n"])), "big"),
        )
        return numbers.public_key()
    except (ValueError, TypeError) as exc:
        raise ValueError("email or password is wrong") from exc


def _begin_saml(db: Database, root: Path, tenant_id: str, row: dict, redirect_uri: str) -> str:
    meta = _saml_metadata(str(row["metadata_url"] or row["issuer"]))
    state = secrets.token_urlsafe(24)
    _pending[state] = (time.time() + _TTL, tenant_id, "", "", "saml", "", "")
    _drop_expired()
    request_id = "_" + secrets.token_hex(16)
    issued = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    issuer = str(row["client_id"] or row["issuer"])
    xml = (
        "<samlp:AuthnRequest"
        ' xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"'
        ' xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"'
        f' ID="{request_id}" Version="2.0" IssueInstant="{issued}"'
        f' Destination="{_xml(meta["sso"])}"'
        f' AssertionConsumerServiceURL="{_xml(redirect_uri)}"'
        f' ProtocolBinding="{_SAML_POST}">'
        f"<saml:Issuer>{_xml(issuer)}</saml:Issuer>"
        "</samlp:AuthnRequest>"
    )
    raw = base64.b64encode(zlib.compress(xml.encode("utf-8"))[2:-4]).decode("ascii")
    query = urlencode({"SAMLRequest": raw, "RelayState": state})
    return f"{meta['sso']}?{query}"


def _saml_email(row: dict, encoded: str) -> str:
    try:
        xml = base64.b64decode(encoded)
    except (ValueError, TypeError) as exc:
        raise ValueError("email or password is wrong") from exc
    meta = _saml_metadata(str(row["metadata_url"] or row["issuer"]))
    return _verified_assertion_email(xml, meta["cert"])


def _saml_metadata(url: str) -> dict:
    target = _https(url)
    try:
        response = httpx.get(target, timeout=20)
    except httpx.HTTPError as exc:
        raise ValueError("this sign-in could not be checked") from exc
    if response.status_code >= 400 or not response.content:
        raise ValueError("this sign-in could not be checked")
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError as exc:
        raise ValueError("this sign-in could not be checked") from exc
    sso = ""
    cert = ""
    for node in root.iter():
        binding = node.attrib.get("Binding", "")
        location = node.attrib.get("Location", "")
        if binding == _SAML_REDIRECT and location.startswith(_HTTPS) and not sso:
            sso = location
        if node.tag.endswith("X509Certificate") and node.text and not cert:
            cert = "".join(node.text.split())
    if not sso or not cert:
        raise ValueError("this sign-in could not be checked")
    return {"sso": sso, "cert": cert}


def _verified_assertion_email(xml: bytes, cert_b64: str) -> str:
    """Check the assertion signature with the metadata certificate, then read the email."""
    try:
        root = ElementTree.fromstring(xml)
        der = base64.b64decode(cert_b64)
        certificate = load_der_x509_certificate(der)
    except (ElementTree.ParseError, ValueError, TypeError) as exc:
        raise ValueError("email or password is wrong") from exc
    signature = _child(root, "Signature")
    if signature is None:
        for node in root.iter():
            if node.tag.endswith("Assertion"):
                signature = _child(node, "Signature")
                if signature is not None:
                    root = node
                    break
    if signature is None:
        raise ValueError("email or password is wrong")
    info = _child(signature, "SignedInfo")
    value = _child(signature, "SignatureValue")
    if info is None or value is None or not (value.text or "").strip():
        raise ValueError("email or password is wrong")
    reference = _child(info, "Reference")
    digest_node = None
    if reference is not None:
        digest_node = _child(reference, "DigestValue")
    if digest_node is None or not (digest_node.text or "").strip():
        raise ValueError("email or password is wrong")
    method = ""
    for node in info.iter():
        if node.tag.endswith("SignatureMethod"):
            method = node.attrib.get("Algorithm", "")
    if "rsa-sha256" not in method:
        raise ValueError("email or password is wrong")
    signed = _exclusive(root, skip_signature=True)
    digest = base64.b64encode(hashlib.sha256(signed).digest()).decode("ascii")
    if digest != "".join((digest_node.text or "").split()):
        raise ValueError("email or password is wrong")
    info_bytes = _exclusive(info, skip_signature=False)
    try:
        signature_bytes = base64.b64decode("".join((value.text or "").split()))
        public = certificate.public_key()
        if not isinstance(public, rsa.RSAPublicKey):
            raise ValueError("email or password is wrong")
        public.verify(signature_bytes, info_bytes, padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:
        raise ValueError("email or password is wrong") from exc
    email = _assertion_email(root)
    if not email:
        raise ValueError("email or password is wrong")
    return email


def _assertion_email(root: ElementTree.Element) -> str:
    for node in root.iter():
        name = node.attrib.get("Name", "")
        if node.tag.endswith("Attribute") and name in {"email", "mail", "emailaddress"}:
            for child in list(node):
                if child.tag.endswith("AttributeValue") and (child.text or "").strip():
                    return child.text.strip()
        if node.tag.endswith("NameID") and (node.text or "").strip() and "@" in node.text:
            return node.text.strip()
    return ""


def _child(node: ElementTree.Element, name: str) -> ElementTree.Element | None:
    for child in list(node):
        if child.tag.endswith(name):
            return child
    return None


def _exclusive(node: ElementTree.Element, *, skip_signature: bool) -> bytes:
    """Exclusive canonical form of one element. Namespaces in use are declared on it."""
    return _render(node, _inherited(node), skip_signature=skip_signature).encode("utf-8")


def _inherited(node: ElementTree.Element) -> dict[str, str]:
    """Namespaces declared by ancestors. ElementTree does not keep those on the node."""
    return {}


def _render(node: ElementTree.Element, inherited: dict[str, str], *, skip_signature: bool) -> str:
    if skip_signature and node.tag.endswith("Signature"):
        return ""
    tag = node.tag
    xmlns = ""
    local = tag
    if tag.startswith("{"):
        uri, local = tag[1:].split("}", 1)
        xmlns = uri
    prefix = _prefix(node, xmlns) if xmlns else ""
    name = f"{prefix}:{local}" if prefix else local
    parent = dict(inherited)
    declared: list[tuple[str, str]] = []
    if xmlns and parent.get(prefix) != xmlns:
        attr = "xmlns" if not prefix else f"xmlns:{prefix}"
        declared.append((attr, xmlns))
        parent[prefix] = xmlns
    attrs: list[tuple[str, str]] = []
    for key, value in node.attrib.items():
        if key.startswith("{http://www.w3.org/2000/xmlns/}"):
            continue
        if key.startswith("{"):
            uri, attr_local = key[1:].split("}", 1)
            attr_prefix = _prefix(node, uri)
            attrs.append((f"{attr_prefix}:{attr_local}" if attr_prefix else attr_local, value))
            if parent.get(attr_prefix) != uri:
                label = "xmlns" if not attr_prefix else f"xmlns:{attr_prefix}"
                declared.append((label, uri))
                parent[attr_prefix] = uri
        else:
            attrs.append((key, value))
    declared.sort()
    attrs.sort()
    rendered = "".join(f' {key}="{_xml(value)}"' for key, value in [*declared, *attrs])
    children = "".join(
        _render(child, parent, skip_signature=skip_signature) for child in list(node)
    )
    text = _xml(node.text or "")
    if not children and not text:
        return f"<{name}{rendered}></{name}>"
    tail = text + children
    return f"<{name}{rendered}>{tail}</{name}>"


def _prefix(node: ElementTree.Element, uri: str) -> str:
    if not uri:
        return ""
    for key, value in node.attrib.items():
        if key.startswith("{http://www.w3.org/2000/xmlns/}") and value == uri:
            return key.rsplit("}", 1)[-1]
    if uri == "http://www.w3.org/2000/09/xmldsig#":
        return "ds"
    if uri == "urn:oasis:names:tc:SAML:2.0:assertion":
        return "saml"
    return "ns"


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _https(url: str) -> str:
    if not url.startswith(_HTTPS) or " " in url:
        raise ValueError("this sign-in could not be checked")
    return url


def _pkce(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _b64(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _drop_expired() -> None:
    now = time.time()
    for key, pending in list(_pending.items()):
        if pending[0] < now:
            _pending.pop(key, None)
