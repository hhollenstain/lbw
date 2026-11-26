.PHONY: build test dist image clean

VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
LDFLAGS := -s -w -X main.version=$(VERSION)
TARGETS := darwin/arm64 darwin/amd64 linux/amd64 linux/arm64 windows/amd64

# The macOS menu bar icon is drawn through AppKit, which needs cgo. Linux and
# Windows draw theirs without it, so they still build from any machine.
HOST_OS := $(shell go env GOOS)
CGO_HERE := $(if $(filter darwin,$(HOST_OS)),1,0)

build:
	CGO_ENABLED=$(CGO_HERE) go build -trimpath -ldflags "$(LDFLAGS)" -o bin/lbw-runner ./cmd/lbw-runner

test:
	go vet ./...
	go test ./...

dist:
	@mkdir -p dist
	@for target in $(TARGETS); do \
		os=$${target%/*}; arch=$${target#*/}; ext=""; \
		if [ "$$os" = windows ]; then ext=".exe"; fi; \
		echo "dist/lbw-runner-$$os-$$arch$$ext"; \
		cgo=0; cc=""; \
		if [ "$$os" = darwin ]; then \
			if [ "$(HOST_OS)" = darwin ]; then \
				clangarch=arm64; if [ "$$arch" = amd64 ]; then clangarch=x86_64; fi; \
				cgo=1; cc="clang -arch $$clangarch"; \
			else \
				echo "  built without the menu bar icon: build macOS on a Mac for it"; \
			fi; \
		fi; \
		CGO_ENABLED=$$cgo CC="$$cc" GOOS=$$os GOARCH=$$arch go build -trimpath -ldflags "$(LDFLAGS)" \
			-o dist/lbw-runner-$$os-$$arch$$ext ./cmd/lbw-runner || exit 1; \
	done
	@cd dist && (command -v sha256sum >/dev/null && sha256sum lbw-runner-* || shasum -a 256 lbw-runner-*) > SHA256SUMS

image:
	docker build --build-arg VERSION=$(VERSION) -t lbw-runner:$(VERSION) .

clean:
	rm -rf bin dist
