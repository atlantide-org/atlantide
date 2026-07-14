# Publishing and consuming components

A **component** is a library-authored L2 construct — several resources behind one
parameterized object (see [`atlantide/core/component.py`](../../atlantide/core/component.py)).
This example shows how to *publish* one in a public git repo and *consume* it from
another project.

## Why not a URL import

Atlas-lang config is a deterministic sandbox: it may import only `atlantide.*` and
cannot touch the network (see [`atlantide/lang/validate.py`](../../atlantide/lang/validate.py)),
which is what keeps the IR byte-stable. So there is no live URL import. Instead you
fetch once — pinned to an exact commit and a content hash — and import the vendored
code locally. It's the `terraform init` model, and the vendored package mounts under
`atlantide.components.<alias>`, a namespace the sandbox already permits, so no
sandbox rule changes.

## The two sides

- [`publishable/secure_site/`](publishable/secure_site/__init__.py) — what an author
  puts in a public repo: a `Component` subclass (ordinary trusted Python).
- [`consumer/`](consumer/) — a project that imports it as `atlantide.components.site`.

## Try it locally (no external repo needed)

Turn the publishable package into a throwaway git repo, then consume it:

```bash
# 1. make the publishable component a local git repo pinned at a tag
REPO=$(mktemp -d)
cp -r publishable/secure_site "$REPO"/
git -C "$REPO" init -q && git -C "$REPO" add -A
git -C "$REPO" -c user.email=t@t -c user.name=t commit -qm init
git -C "$REPO" tag v1

# 2. from the consumer project, fetch + pin + vendor it under the alias "site"
cd consumer
uv run atlantide component add "file://$REPO" --ref v1 --as site --subdir secure_site

# 3. the config now imports atlantide.components.site — plan it
uv run atlantide component verify        # vendored tree matches the lock
uv run atlantide plan                    # SecureSite('cdn', ...) expands to its children
```

`add` writes the `[components.site]` source into `atlantide.toml`, resolves the ref
to a commit, and vendors the tree into `.atlantis/components/site/` with the pin
recorded in `atlantide.lock`. Commit `atlantide.lock`; git-ignore `.atlantis/`
(it's derived — `atlantide component vendor` rebuilds it from the lock).

## Trust

A published component runs as trusted Python, like a provider — vet what you add.
Integrity after that rests on the pin: `atlantide component verify` re-hashes the
vendored tree and fails on any tamper or drift, and `atlantide build` records each
component's commit in the `.atlas` artifact as provenance.
