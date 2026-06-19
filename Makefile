-include .env

MERIDIAN_WEB ?= ../meridian-web

.PHONY: build-frontend

# Build frontend assets from the sibling meridian-web checkout
build-frontend:
	cd $(MERIDIAN_WEB) && pnpm build
	rsync -a --delete $(MERIDIAN_WEB)/dist/ src/meridian/web_dist/
	@echo "Frontend assets copied to src/meridian/web_dist/"
