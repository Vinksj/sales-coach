"""One-off, for an install that predates the setup wizard: move the seller's identity out of the
tracked files into the user settings.

A fresh install does not need this: fill the form at /setup instead.

Before the project was generalised, an install kept its seller's name, addresses, domains, style
guide and accounts in tracked files, and a few sentences about the offering inside the prompts.
This script writes them to <install>/data/settings/ (gitignored), where salescoach.config reads
them on top of the tracked defaults, so that install behaves exactly as before.

    python scripts/migrate_personal_settings.py /path/to/install [--profile FILE] [--dry-run] [--from-rev REV]

What it writes, each ONLY if the file does not exist yet (it never overwrites, so it is safe to run
twice, and safe to run before or after the install is updated to the generic code):

    seller.yaml     the seller profile (name, emails, company, offering, languages, ...)
    style.md        the style guide, copied as it was
    accounts.yaml   the accounts, copied as they were

Where the values come from: the install's own config/ files while they still carry them; once the
install has the generic files, the same files at --from-rev (the last commit before the generic
config) via `git show`. What used to live inside prompts (company, role, website, offering, who
the buyers are, how the seller sells) cannot be recovered from a file, so it comes from --profile,
a YAML file with any of the PROFILE_DEFAULTS keys below. Without --profile those fields get
placeholder text, and the script says so: correct them at /setup afterwards.

It reads no secret and no token file, and it touches nothing outside <install>/data/settings/.
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

PRE_PHASE_A_REV = "e9c48af"

# What used to be written into the prompts themselves. No person or company is named in this
# repository, so these are placeholders; pass --profile FILE to supply the real values.
PLACEHOLDER = "(not migrated: fill this in at /setup)"
PROFILE_DEFAULTS = {
    "company": PLACEHOLDER,
    "role": "",
    "website": "",
    "offering": PLACEHOLDER,
    "icp": "",
    "buyer_titles": "",
    "vocabulary": "",
    "languages": ["en", "hi"],          # what the pre-wizard install was tuned on
    "timezone": "Asia/Kolkata",         # seller.DEFAULT_TIMEZONE
    "call_context": "",
}
SIGNATURE_MARKER = "Customer and prospect emails use the full signature:"


class _Dumper(yaml.SafeDumper):
    pass


# A multi-line value (the signature) as a literal block, so the file stays readable and editable.
_Dumper.add_representer(str, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="|" if "\n" in v else None))


class MigrationError(RuntimeError):
    pass


def _git_show(install: Path, rev: str, rel: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(install), "show", f"{rev}:{rel}"], capture_output=True, text=True,
                             timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _source(install: Path, rev: str, rel: str, is_personal) -> tuple[str, str]:
    """(text, where it came from) for one tracked file: the working tree while it is still the
    personal version, else the pre-Phase-A revision."""
    path = install / rel
    if path.exists():
        text = path.read_text()
        if is_personal(text):
            return text, str(path)
    text = _git_show(install, rev, rel)
    if text is not None and is_personal(text):
        return text, f"git {rev}:{rel}"
    raise MigrationError(f"{rel}: the personal version is neither in {install} nor at revision {rev}. "
                         "Pass --from-rev with the last commit before the generic config.")


def _yaml(text: str) -> dict:
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def _signature(style: str) -> str:
    if SIGNATURE_MARKER not in style:
        raise MigrationError("style.md: no sign-off section found; cannot take the signature from it")
    return style.split(SIGNATURE_MARKER, 1)[1].strip("\n").rstrip()


def load_profile(path=None) -> dict:
    """PROFILE_DEFAULTS with the --profile file's values on top. Unknown keys are refused."""
    values = dict(PROFILE_DEFAULTS)
    if path:
        try:
            given = _yaml(Path(path).expanduser().read_text())
        except (OSError, yaml.YAMLError) as exc:
            raise MigrationError(f"--profile {path}: cannot be read ({type(exc).__name__})") from None
        unknown = sorted(set(given) - set(PROFILE_DEFAULTS))
        if unknown:
            raise MigrationError(f"--profile {path}: unknown field(s) {', '.join(unknown)}; "
                                 f"known: {', '.join(PROFILE_DEFAULTS)}")
        values.update({k: v for k, v in given.items() if v not in (None, "", [])})
    return values


def build(install: Path, rev: str, profile: dict | None = None) -> dict[str, str]:
    """{file name: content} for the user settings folder."""
    from_prompts = dict(PROFILE_DEFAULTS) if profile is None else profile
    style, _ = _source(install, rev, "config/style.md", lambda t: "{{" not in t and SIGNATURE_MARKER in t)
    accounts, _ = _source(install, rev, "config/accounts.yaml", lambda t: bool(_yaml(t).get("accounts")))
    automation, _ = _source(install, rev, "config/automation.yaml", lambda t: bool(_yaml(t).get("my_addresses")))
    policy_path = install / "config/policy.yaml"
    policy = _yaml(policy_path.read_text()) if policy_path.exists() else {}
    name = (policy.get("email") or {}).get("from_name")
    if not name:
        old_policy = _git_show(install, rev, "config/policy.yaml")
        name = (_yaml(old_policy).get("email") or {}).get("from_name") if old_policy else None
    if not name:
        raise MigrationError("config/policy.yaml has no email.from_name, now or at the old revision, so the seller's "
                             "name is unknown. Fill the form at /setup instead.")

    auto = _yaml(automation)
    seller = {
        "name": name,
        "emails": [str(a).lower().strip() for a in auto["my_addresses"]],
        "company": from_prompts["company"],
        "role": from_prompts["role"],
        "website": from_prompts["website"],
        "offering": from_prompts["offering"],
        "icp": from_prompts["icp"],
        "buyer_titles": from_prompts["buyer_titles"],
        "vocabulary": from_prompts["vocabulary"],
        "own_domains": [str(d).lower().strip() for d in ((auto.get("calendar") or {}).get("own_domains") or [])],
        "languages": from_prompts["languages"],
        "timezone": from_prompts["timezone"],
        "signature": _signature(style),
        "call_context": from_prompts["call_context"],
    }
    header = ("# Seller profile, migrated from the tracked files by scripts/migrate_personal_settings.py.\n"
              "# Edit it at /setup or here; this file wins over config/seller.yaml.\n")
    return {
        "seller.yaml": header + yaml.dump(seller, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                                         default_flow_style=False, width=110),
        "style.md": style,
        "accounts.yaml": accounts,
    }


def _write_new(path: Path, content: str) -> bool:
    """Create `path` with `content`, atomically, only if it does not exist. True when written."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.link(tmp, path)              # fails if someone created it meanwhile: never an overwrite
    except FileExistsError:
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return True


def migrate(install, rev: str = PRE_PHASE_A_REV, dry_run: bool = False, out=print, profile=None) -> dict[str, str]:
    """Returns {file name: 'written' | 'exists' | 'would write'}. `profile` is the path of a --profile file."""
    install = Path(install).expanduser().resolve()
    if not (install / "config").is_dir() or not (install / "salescoach").is_dir():
        raise MigrationError(f"{install} does not look like a sales coach install (no config/ and salescoach/)")
    settings = install / "data" / "settings"
    values = load_profile(profile)
    wanted = build(install, rev, values)
    placeholders = sorted(k for k, v in values.items() if v == PLACEHOLDER)
    if placeholders:
        out(f"  note: {', '.join(placeholders)} will hold placeholder text (no --profile value); correct at /setup")
    result = {}
    for name, content in wanted.items():
        target = settings / name
        if target.exists():
            result[name] = "exists"
            same = target.read_text() == content
            out(f"  {target}: exists, left untouched" + ("" if same else " (differs from what this script would write)"))
        elif dry_run:
            result[name] = "would write"
            out(f"  {target}: would write {len(content.splitlines())} lines")
        else:
            result[name] = "written" if _write_new(target, content) else "exists"
            out(f"  {target}: {result[name]}")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("install", help="path of the sales coach install to migrate")
    parser.add_argument("--from-rev", default=PRE_PHASE_A_REV,
                        help=f"last commit with the personal config files (default {PRE_PHASE_A_REV})")
    parser.add_argument("--profile", metavar="FILE",
                        help="YAML with what used to live in the prompts: " + ", ".join(PROFILE_DEFAULTS))
    parser.add_argument("--dry-run", action="store_true", help="say what would be written, write nothing")
    args = parser.parse_args(argv)
    try:
        result = migrate(args.install, rev=args.from_rev, dry_run=args.dry_run, profile=args.profile)
    except MigrationError as exc:
        print(f"Not migrated: {exc}", file=sys.stderr)
        return 1
    if all(v == "exists" for v in result.values()):
        print("Nothing to do: the user settings are already there.")
    elif not args.dry_run:
        print("Done. Restart `salescoach serve` (or let launchd restart it) and open / : it should not ask for setup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
