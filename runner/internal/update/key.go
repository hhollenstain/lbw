package update

import (
	"crypto/ed25519"
	"encoding/base64"
)

// releaseKey is the public half of LBW Runner's release key, base64. The
// private half signs each release's SHA256SUMS in the release workflow
// (RUNNER_SIGNING_KEY). To replace it, run `lbw-runner-sign keygen`, put the
// printed key here, and update the secret: runners built before then keep
// trusting the old key, so they must be updated by hand once.
const releaseKey = "C5WD0iU2cvxjuDYfgLJDbvQLGExs7x26sgO/iEbNI3A="

// PublicKey is the key a release must be signed with.
var PublicKey = func() ed25519.PublicKey {
	raw, err := base64.StdEncoding.DecodeString(releaseKey)
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil
	}
	return raw
}()
