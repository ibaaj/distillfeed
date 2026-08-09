# Publish DistillFeed 0.23.0 to the release repository

These commands update the existing Git checkout while preserving its `.git`
directory. Review or commit any current work in the checkout before starting.

```bash
cd ~/repos/distillfeed_release
git status --short
```

Extract `distillfeed-source-0.23.0.zip` to a temporary directory, then copy the
release tree into the checkout:

```bash
tmp_dir="$(mktemp -d)"
unzip /path/to/distillfeed-source-0.23.0.zip -d "$tmp_dir"
rsync -a --delete --exclude='.git/' "$tmp_dir/distillfeed/" ~/repos/distillfeed_release/
cd ~/repos/distillfeed_release
git status --short
git diff --check
```

Run the project tests in the repository's normal development environment. If
the results are satisfactory, publish the release:

```bash
git add -A
git commit -m "Release DistillFeed 0.23.0"
git tag -a v0.23.0 -m "DistillFeed 0.23.0"
git push origin main
git push origin v0.23.0
```

To update the personal instance after stopping DistillFeed and its scheduled
jobs, place `upd.sh` and `distillfeed-0.23.0.tar.gz` in the same directory and
run:

```bash
chmod +x /path/to/upd.sh
/path/to/upd.sh ~/repos/distillfeed_perso
```

The updater preserves the instance configuration, database, OPML, secrets, and
arXiv configuration and creates a backup before switching source files.
