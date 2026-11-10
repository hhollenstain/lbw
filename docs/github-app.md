# The LBW GitHub App

LBW uses one GitHub App. The person who runs LBW creates it, makes it
public, and saves the private key once. Each LBW organization installs
that same app on the GitHub account that owns its repositories. An
organization does not create an app and does not receive the private key.
Their steps are in [github-organization.md](github-organization.md).

An install is one GitHub account. LBW stores that installation on one LBW
organization. Sync, the repository list, and checkout use that installation
only. The installation token cannot see another GitHub account, and LBW
writes teams and repository grants only on the LBW organization that owns
the installation. The token is minted when a push, a pull request, a merge,
or a sync needs it, and it is not stored.

## Create the app

Sign in to GitHub as the account that will own the app. Open
[New GitHub App](https://github.com/settings/apps/new).

- GitHub App name: a name people will recognize on the install screen,
  such as LBW.
- Homepage URL: the address of this LBW install, for example
  `https://lbw.example.com`.
- Callback URL: the organization site, not the engineer host. The
  engineer dashboard shows that address. Connecting GitHub and signing
  in with GitHub both return there. On this machine the organization
  site is `https://www-local.lookbothways.dev`, so the callback is
  `https://www-local.lookbothways.dev/api/github/callback`.
- Leave "Expire user authorization tokens" at GitHub's default.
- Turn on "Request user authorization (OAuth) during installation" so a
  person can connect from their personal page.
- Webhook: leave Active unchecked. Leave the webhook URL empty. LBW does
  not receive webhook events.
- Where can this GitHub App be installed: Any account.

Repository permissions:

| Permission | Access |
| --- | --- |
| Contents | Read and write |
| Issues | Read and write |
| Pull requests | Read and write |
| Metadata | Read-only |

GitHub adds Metadata on its own. Contents and Pull requests need write so
LBW can push commits, open a pull request, and merge it.

Organization permissions:

| Permission | Access |
| --- | --- |
| Members | Read-only |

Members read is how LBW sees organization role, teams, and which
repositories a team can use.

Leave account permissions empty. Leave every event unchecked, since the
webhook is off.

Create the app. On its settings page, choose Make public so another
GitHub account can install it. A private app can be installed only by
the account that owns it.

## Save the key on the engineer dashboard

On the app's settings page, note the App ID. Generate a private key and
a client secret. GitHub shows the client id on the same page.

An LBW engineer opens the engineer dashboard and signs in there. An
organization admin does not use this host. Locally the organization site is `https://www-local.lookbothways.dev`
and the engineer dashboard is `https://crossing-local.lookbothways.dev`.
Both names resolve to `127.0.0.1`. Nginx presents the certificate from
`make certs`. The dashboard is its own service.

The callback URL on the engineer page is the organization site,
`https://www-local.lookbothways.dev/api/github/callback`. Put that on
the GitHub App. Connecting GitHub and signing in with GitHub return
there.

Paste the App ID, the private key, and the client secret, and save. They
are sealed with the install key. They are not shown again, and they do
not go in git, in `.lbw/`, or on an organization's page.

## Keep a local copy

`make wipe` deletes the database, so the sealed app is gone. After the
app is saved once, export it to `github.local.yaml` in this repository.
That file is gitignored. It holds the app id, the private key, the client
id, the client secret, and the chosen installation. It does not hold an
installation token.

```bash
make export-github
```

Bring the stack back with `make up-local`, then restore the app:

```bash
make hydrate
```

Hydrate seals the file onto the install. It does not call GitHub. When
the database is empty it creates the local organization first. The setup
page still asks for the admin password. The store root for the Docker
stack is `./workspaces`, because that folder holds `.lbw/master.key`.

`lbw export-github` and `lbw hydrate` take `--workspace` and `--file`
when the database is not the local Postgres port. The file must stay
untracked, and it must not live under `.lbw/`.

After the save, an organization installs the app from its own GitHub tab.
The install link has the form
`https://github.com/apps/<slug>/installations/new`. Send them
[github-organization.md](github-organization.md).

## One installation, one LBW organization

Refresh lists the installations of this app. Each LBW organization
selects the GitHub account it installed, and only that account. Two LBW
organizations do not share an installation. Sync then copies that
account's organization role, teams, and repository access onto people in
that LBW organization who have connected GitHub. Another organization's
projects stay as they are.

The LBW owner stays owner. A GitHub team is remembered by its slug, so a
rename keeps the team. Admin or maintain on a repository becomes manage.
Push, triage, or pull becomes use. A folder or a plain git checkout is
left alone.
