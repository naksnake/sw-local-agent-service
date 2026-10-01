"""Host allowlist, remote addresses, sealed credentials, remotes and redaction (§5.7)."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from slas_git.credentials import (
    AesGcmSealer,
    AuthType,
    CredentialError,
    EncryptedFileStore,
    FakeSealer,
    derive_key,
    secret_key_from_env,
    token_fingerprint,
)
from slas_git.hosts import (
    DEFAULT_GIT_HOSTS,
    GIT_HOSTS_FILE_HEADER,
    GitHost,
    GitHosts,
    GitHostsError,
    HostNotAllowedError,
    default_hosts,
    host_for,
    hosts_from_mapping,
    parse_remote_uri,
    render_git_hosts_yaml,
)
from slas_git.redact import find_secrets, redact
from slas_git.remotes import Remote, RemoteError, RemoteStore, add_remote

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 14, 9, tzinfo=UTC)
SECRET_KEY = "k" * 43
KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


def test_git_hosts_yaml_is_rendered_from_code_and_lists_the_internal_hosts() -> None:
    expected = render_git_hosts_yaml(DEFAULT_GIT_HOSTS, header=GIT_HOSTS_FILE_HEADER)
    assert (REPO_ROOT / "config" / "git-hosts.yaml").read_text(encoding="utf-8") == expected
    hosts = default_hosts()
    assert hosts.allowed_names() == ["gitlab.internal", "gitea.internal"]
    assert hosts.find("GITLAB.internal") is not None and hosts.find("github.com") is None
    assert hosts.hosts[0].sentence() == "gitlab.internal: GitLab over https and ssh."
    with pytest.raises(GitHostsError, match="could not be used"):
        hosts_from_mapping(
            {"hosts": [{"name": "a", "hostname": "a.internal", "protocols": ["http"]}]}
        )
    with pytest.raises(GitHostsError) as twice:
        hosts_from_mapping(
            {
                "hosts": [
                    {"name": "a", "hostname": "x.internal"},
                    {"name": "b", "hostname": "X.internal"},
                ]
            }
        )
    assert "listed twice" in twice.value.message.likely_cause


def test_remote_addresses_parse_and_never_carry_a_credential() -> None:
    https = parse_remote_uri("https://gitlab.internal/firmware/bmc.git")
    assert (https.scheme, https.hostname, https.path) == (
        "https",
        "gitlab.internal",
        "firmware/bmc",
    )
    assert https.https_url("oauth2") == "https://oauth2@gitlab.internal/firmware/bmc.git"
    assert https.owner_and_repo == ("firmware", "bmc")
    scp = parse_remote_uri("git@gitea.internal:lab/tools")
    assert (
        scp.scheme == "ssh"
        and scp.ssh_user == "git"
        and scp.ssh_url() == "ssh://git@gitea.internal/lab/tools.git"
    )
    loop = parse_remote_uri("http://127.0.0.1:8123/firmware/bmc.git")
    assert loop.port == 8123 and loop.https_url() == "http://127.0.0.1:8123/firmware/bmc.git"
    with pytest.raises(GitHostsError) as token:
        parse_remote_uri("https://oauth2:glpat-secret@gitlab.internal/firmware/bmc.git")
    assert token.value.message.what_happened == "The address carries a credential."
    with pytest.raises(GitHostsError, match="is not a Git address"):
        parse_remote_uri("http://gitlab.internal/firmware/bmc.git")  # plain http off the loopback
    with pytest.raises(GitHostsError, match="has no group/project path"):
        parse_remote_uri("https://gitlab.internal/bmc")
    with pytest.raises(GitHostsError, match="is not a Git address"):
        parse_remote_uri("ftp://gitlab.internal/a/b")


def test_host_allowlist_rejects_with_the_sentence_from_claude_md() -> None:
    hosts = default_hosts()
    with pytest.raises(HostNotAllowedError) as raised:
        host_for(parse_remote_uri("https://github.com/octo/repo.git"), hosts)
    assert raised.value.message.what_happened == (
        "github.com is not an allowed Git host. Allowed: gitlab.internal, gitea.internal."
    )
    assert host_for(parse_remote_uri("https://gitea.internal/lab/tools.git"), hosts).kind == "gitea"
    with pytest.raises(GitHostsError, match="does not allow ssh"):
        host_for(parse_remote_uri("git@gitea.internal:lab/tools.git"), hosts)
    with pytest.raises(GitHostsError, match="has no pinned SSH host key"):
        host_for(parse_remote_uri("git@gitlab.internal:firmware/bmc.git"), hosts)
    pinned = GitHosts(
        hosts=[
            GitHost(
                name="gl",
                hostname="gitlab.internal",
                protocols=["ssh"],
                ssh_host_key="gitlab.internal ssh-ed25519 AAAA",
            )
        ]
    )
    assert host_for(parse_remote_uri("git@gitlab.internal:firmware/bmc.git"), pinned).name == "gl"


def test_key_derivation_is_deterministic_and_purpose_bound() -> None:
    assert derive_key(SECRET_KEY) == derive_key(SECRET_KEY) and len(derive_key(SECRET_KEY)) == 32
    assert derive_key(SECRET_KEY) != derive_key(SECRET_KEY, purpose=b"other")
    assert derive_key(SECRET_KEY) != derive_key("j" * 43)
    with pytest.raises(CredentialError, match="too short"):
        derive_key("short")
    with pytest.raises(CredentialError, match="is not set"):
        secret_key_from_env({})
    assert secret_key_from_env({"SLAS_SECRET_KEY": SECRET_KEY}) == SECRET_KEY


def test_fake_sealer_round_trips_and_detects_tampering() -> None:
    sealer = FakeSealer(derive_key(SECRET_KEY))
    sealed = sealer.seal(b"glpat-abcdefghijklmnopqrst", aad=b"cred-1|pat")
    assert b"glpat" not in sealed
    assert sealer.open(sealed, aad=b"cred-1|pat") == b"glpat-abcdefghijklmnopqrst"
    with pytest.raises(CredentialError, match="could not be opened"):
        sealer.open(sealed, aad=b"cred-1|someone-else")
    with pytest.raises(CredentialError, match="could not be opened"):
        sealer.open(sealed[:-1] + bytes([sealed[-1] ^ 1]), aad=b"cred-1|pat")


def test_aes_gcm_sealer_seals_and_refuses_a_wrong_owner_or_altered_ciphertext() -> None:
    """AES-256-GCM via `cryptography` (ADR-0016): the production sealer of the git broker."""
    sealer = AesGcmSealer(derive_key(SECRET_KEY))
    sealed = sealer.seal(b"glpat-secret", aad=b"cred-1|pat")
    assert b"glpat-secret" not in sealed
    assert sealer.open(sealed, aad=b"cred-1|pat") == b"glpat-secret"
    assert sealer.seal(b"glpat-secret", aad=b"cred-1|pat") != sealed  # a fresh nonce every time
    with pytest.raises(Exception, match=""):
        sealer.open(sealed, aad=b"cred-1|someone-else")
    with pytest.raises(ValueError, match="32-byte key"):
        AesGcmSealer(b"short")


def test_encrypted_store_keeps_ciphertext_by_reference_and_scopes_by_owner(tmp_path: Path) -> None:
    store = EncryptedFileStore(
        tmp_path / "Git" / "credentials.json", FakeSealer(derive_key(SECRET_KEY))
    )
    ref = store.put(
        "glpat-abcdefghijklmnopqrst", owner="pat", auth_type="pat", fingerprint="…prst", now=NOW
    )
    assert ref.startswith("cred-") and store.fingerprint_of(ref) == "…prst"
    raw = (tmp_path / "Git" / "credentials.json").read_text(encoding="utf-8")
    assert (
        "glpat-abcdefghijklmnopqrst" not in raw
        and find_secrets(raw, ["glpat-abcdefghijklmnopqrst"]) == []
    )
    assert stat.S_IMODE((tmp_path / "Git" / "credentials.json").stat().st_mode) == 0o600
    assert store.reveal(ref, owner="pat") == "glpat-abcdefghijklmnopqrst"
    with pytest.raises(CredentialError, match="not available"):
        store.reveal(ref, owner="lee")
    store.rotate(ref, "glpat-zyxwvutsrqponmlkjihg", owner="pat", fingerprint="…jihg", now=NOW)
    assert store.reveal(ref, owner="pat") == "glpat-zyxwvutsrqponmlkjihg"
    assert json.loads(raw)[ref]["sealer"] == "fake-for-tests"
    store.delete(ref, owner="pat")
    with pytest.raises(CredentialError, match="not available"):
        store.reveal(ref, owner="pat")
    assert store.fingerprint_of("cred-000000000000000000000000") == "unknown"


def test_add_remote_validates_and_stores_only_a_reference(tmp_path: Path) -> None:
    credentials = EncryptedFileStore(
        tmp_path / "credentials.json", FakeSealer(derive_key(SECRET_KEY))
    )
    remotes = RemoteStore(tmp_path / "remotes.json")
    hosts = GitHosts(
        hosts=[
            GitHost(
                name="gl",
                hostname="gitlab.internal",
                kind="gitlab",
                api_base="https://gitlab.internal/api/v4",
            ),
            GitHost(
                name="ssh",
                hostname="ssh.internal",
                protocols=["ssh"],
                ssh_host_key="ssh.internal ssh-ed25519 AAAA",
            ),
        ]
    )

    def add(
        name: str, uri: str, auth_type: AuthType, secret: str, ssh_fingerprint: str | None = None
    ) -> tuple[Remote, GitHost]:
        return add_remote(
            owner="pat",
            name=name,
            uri=uri,
            auth_type=auth_type,
            secret=secret,
            hosts=hosts,
            credentials=credentials,
            remotes=remotes,
            now=NOW,
            ssh_fingerprint=ssh_fingerprint,
        )

    remote, host = add(
        "gitlab-firmware",
        "https://gitlab.internal/firmware/bmc.git",
        "pat",
        "glpat-abcdefghijklmnopqrst",
    )
    assert (
        host.kind == "gitlab" and remote.fingerprint == "…qrst" and remote.host == "gitlab.internal"
    )
    assert remote.uri == "https://gitlab.internal/firmware/bmc.git"
    assert "credential_ref" not in remote.for_ui() and remote.for_ui()["fingerprint"] == "…qrst"
    assert remote.sentence() == (
        "gitlab-firmware: https://gitlab.internal/firmware/bmc.git, token …qrst, "
        "default branch main."
    )
    stored = (tmp_path / "remotes.json").read_text(encoding="utf-8")
    assert "glpat" not in stored and remote.credential_ref in stored
    assert [r.name for r in remotes.list_for("pat")] == ["gitlab-firmware"] and remotes.list_for(
        "lee"
    ) == []
    with pytest.raises(RemoteError, match="already have a remote called"):
        add(
            "gitlab-firmware",
            "https://gitlab.internal/a/b.git",
            "pat",
            "glpat-abcdefghijklmnopqrst",
        )
    with pytest.raises(RemoteError, match="not a usable remote name"):
        add("Bad Name", "https://gitlab.internal/a/b.git", "pat", "glpat-abcdefghijklmnopqrst")
    with pytest.raises(RemoteError, match="A token goes with an https address"):
        add("kk", "git@ssh.internal:a/b.git", "pat", "glpat-abcdefghijklmnopqrst")
    with pytest.raises(RemoteError, match="does not look like a token"):
        add("kk", "https://gitlab.internal/a/b.git", "pat", KEY)
    with pytest.raises(RemoteError, match="An SSH key goes with an ssh address"):
        add("kk", "https://gitlab.internal/a/b.git", "ssh_key", KEY)
    with pytest.raises(RemoteError, match="is not a private key"):
        add("kk", "git@ssh.internal:a/b.git", "ssh_key", "not a key at all")
    key_remote, _ = add("deploy", "git@ssh.internal:a/b.git", "ssh_key", KEY, "SHA256:abc")
    assert (
        key_remote.uri == "ssh://git@ssh.internal/a/b.git"
        and key_remote.fingerprint == "SHA256:abc"
    )
    assert credentials.reveal(key_remote.credential_ref, owner="pat") == KEY.strip()
    with pytest.raises(RemoteError, match="not one of yours"):
        remotes.get(remote.id, owner="lee")
    assert remotes.delete(remote.id, owner="pat").name == "gitlab-firmware"
    assert token_fingerprint("abc") == "…"


def test_redaction_covers_tokens_keys_urls_and_headers() -> None:
    text = (
        "pushing to https://oauth2:glpat-abcdefghijklmnopqrst@gitlab.internal/a.git with "
        "Authorization: Bearer ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and key\n" + KEY
    )
    clean = redact(text)
    assert "glpat-" not in clean and "ghp_" not in clean and "BEGIN OPENSSH" not in clean
    assert "https://oauth2:[redacted]@gitlab.internal/a.git" in clean
    assert "Authorization: [redacted:authorization_header]" in clean
    assert find_secrets(clean) == [] and find_secrets(text, ["glpat-abcdefghijklmnopqrst"])[
        0
    ].startswith("known secret (glpa…)")
    assert "private_key" in find_secrets(text) and "gitlab_pat" in find_secrets(text)
    assert redact("password=hunter2hunter2") == "password=[redacted:askpass_answer]"
    assert redact("nothing secret here") == "nothing secret here"
