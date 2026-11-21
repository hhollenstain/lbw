.PHONY: build test dist image clean

VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
LDFLAGS := -s -w -X main.version=$(VERSION)
TARGETS := darwin/arm64 darwin/amd64 linux/amd64 linux/arm64 windows/amd64

build:
	CGO_ENABLED=0 go build -trimpath -ldflags "$(LDFLAGS)" -o bin/lbw-runner ./cmd/lbw-runner

test:
	go vet ./...
	go test ./...

dist:
	@mkdir -p dist
	@for target in $(TARGETS); do \
		os=$${target%/*}; arch=$${target#*/}; ext=""; \
		if [ "$$os" = windows ]; then ext=".exe"; fi; \
		echo "dist/lbw-runner-$$os-$$arch$$ext"; \
		CGO_ENABLED=0 GOOS=$$os GOARCH=$$arch go build -trimpath -ldflags "$(LDFLAGS)" \
			-o dist/lbw-runner-$$os-$$arch$$ext ./cmd/lbw-runner || exit 1; \
	done
	@cd dist && (command -v sha256sum >/dev/null && sha256sum lbw-runner-* || shasum -a 256 lbw-runner-*) > SHA256SUMS

image:
	docker build --build-arg VERSION=$(VERSION) -t lbw-runner:$(VERSION) .

clean:
	rm -rf bin dist
