// Command lbw-runner-sign makes and uses LBW Runner's release key.
//
//	lbw-runner-sign keygen PATH     write a new private key to PATH, print the public key
//	lbw-runner-sign sign FILE       write FILE.sig with the key in RUNNER_SIGNING_KEY
//	lbw-runner-sign verify FILE     check FILE.sig against the key built into the runner
//
// The release workflow signs SHA256SUMS with it. The private key lives only
// in that workflow's secret and wherever its owner keeps a backup.
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/hhollenstain/lbw/runner/internal/update"
)

func main() {
	if err := run(os.Args[1:], os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "lbw-runner-sign:", err)
		os.Exit(1)
	}
}

func run(args []string, out io.Writer) error {
	if len(args) != 2 {
		return errors.New("usage: lbw-runner-sign keygen PATH | sign FILE | verify FILE")
	}
	switch args[0] {
	case "keygen":
		public, private, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			return err
		}
		file, err := os.OpenFile(args[1], os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if err != nil {
			return err
		}
		defer file.Close()
		if _, err := fmt.Fprintln(file, base64.StdEncoding.EncodeToString(private.Seed())); err != nil {
			return err
		}
		fmt.Fprintln(out, base64.StdEncoding.EncodeToString(public))
		return nil
	case "sign":
		key, err := privateKey(os.Getenv("RUNNER_SIGNING_KEY"))
		if err != nil {
			return err
		}
		data, err := os.ReadFile(args[1])
		if err != nil {
			return err
		}
		signature := base64.StdEncoding.EncodeToString(ed25519.Sign(key, data))
		return os.WriteFile(args[1]+".sig", []byte(signature+"\n"), 0o644)
	case "verify":
		data, err := os.ReadFile(args[1])
		if err != nil {
			return err
		}
		signature, err := os.ReadFile(args[1] + ".sig")
		if err != nil {
			return err
		}
		if err := update.Verify(update.PublicKey, data, signature); err != nil {
			return err
		}
		fmt.Fprintln(out, "signed by the release key the runner trusts")
		return nil
	}
	return fmt.Errorf("unknown command %q", args[0])
}

// privateKey reads the base64 seed that keygen writes.
func privateKey(text string) (ed25519.PrivateKey, error) {
	seed, err := base64.StdEncoding.DecodeString(strings.TrimSpace(text))
	if err != nil || len(seed) != ed25519.SeedSize {
		return nil, errors.New("RUNNER_SIGNING_KEY is not a key from `lbw-runner-sign keygen`")
	}
	return ed25519.NewKeyFromSeed(seed), nil
}
