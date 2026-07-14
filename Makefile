.PHONY: binary
binary:  ## Build the standalone onefile CLI -> dist/atlantide
	uv run --extra build pyinstaller atlantide.spec --clean --noconfirm
