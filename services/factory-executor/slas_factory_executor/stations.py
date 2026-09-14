"""Admin → Stations (P10): station records, one-time enrolment codes, and the certificate
authority that turns a redeemed code into the station's mTLS identity.

    1  an administrator adds a station and gets a code: `XXXX-XXXX-XXXX`, one use, 15 minutes
    2  on the station: `slas-station-runner enrol --code …` posts {station, code, runner_url}
       to the executor's enrolment endpoint (TLS, server certificate only — the station has
       no certificate yet; it pins the platform CA shipped in its bundle)
    3  the executor checks the code (salted hash, constant time, attempt limit), mints a
       client certificate with the platform CA, creates the station's batch signing key, and
       returns everything once with the station's configuration
    4  the station starts serving with those files; the executor reaches it with its own
       client certificate and signs batches with the per-station key

Codes are stored hashed; a wrong code counts, and five wrong codes lock the station's
enrolment until a new code is issued.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import shutil
import threading
import urllib.parse
from collections.abc import Sequence
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Final, Protocol

from pydantic import Field

from slas_hal.drivers.process import ProcessRunner
from slas_schemas.common import SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.errors import ThreePartMessage
from slas_screen.retention import RetentionPolicy
from slas_station_runner.protocol import EnrolmentGrant, EnrolmentRequest
from slas_station_runner.runner import ScreenTuning, StationConfig, VncSettings

CODE_ALPHABET: Final = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L: read aloud safely


class StationError(RuntimeError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


class Clock(Protocol):
    def now(self) -> datetime: ...


# --- records ------------------------------------------------------------------------------------


class StationRecord(SlasModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    description: str = ""
    allowed_programs: list[str] = Field(default_factory=list)
    state_files: list[str] = Field(default_factory=list)
    versions_command: list[str] = Field(default_factory=list)
    screen: ScreenTuning = Field(default_factory=ScreenTuning)
    retention: RetentionPolicy = Field(default_factory=RetentionPolicy)
    vnc: VncSettings = Field(default_factory=VncSettings)
    runner_url: str | None = None
    enrolled_at: datetime | None = None
    cert_fingerprint: str | None = None
    batch_key_id: str | None = None
    last_seen: datetime | None = None

    @property
    def enrolled(self) -> bool:
        return self.enrolled_at is not None and self.runner_url is not None

    def config(self) -> StationConfig:
        return StationConfig(
            station=self.name,
            allowed_programs=list(self.allowed_programs),
            state_files=list(self.state_files),
            versions_command=list(self.versions_command),
            screen=self.screen,
            retention=self.retention,
            vnc=self.vnc,
        )

    def sentence(self) -> str:
        if self.enrolled and self.enrolled_at is not None:
            return (
                f"{self.name}: enrolled {self.enrolled_at:%Y-%m-%d %H:%M}, runner at "
                f"{self.runner_url}, certificate {self.cert_fingerprint or '?'}."
            )
        return f"{self.name}: not enrolled yet."


class EnrolmentCode(SlasModel):
    station: str
    salt: str
    code_hash: str
    issued_at: datetime
    expires_at: datetime
    issued_by: str
    used_at: datetime | None = None
    attempts: int = 0


class IssuedCode(SlasModel):
    station: str
    code: str
    expires_at: datetime
    sentence: str


class StationRegistry:
    """`Factory/stations.json`: records and the hashed codes, mode 0600, atomic writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> tuple[dict[str, StationRecord], dict[str, EnrolmentCode]]:
        if not self.path.is_file():
            return {}, {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        stations = {n: StationRecord.model_validate(r) for n, r in raw.get("stations", {}).items()}
        codes = {n: EnrolmentCode.model_validate(c) for n, c in raw.get("codes", {}).items()}
        return stations, codes

    def _save(self, stations: dict[str, StationRecord], codes: dict[str, EnrolmentCode]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "stations": {n: r.model_dump(mode="json") for n, r in stations.items()},
            "codes": {n: c.model_dump(mode="json") for n, c in codes.items()},
        }
        write_atomic(self.path, json.dumps(payload, indent=2) + "\n", mode=0o600)

    def list(self) -> list[StationRecord]:
        stations, _ = self._load()
        return sorted(stations.values(), key=lambda r: r.name)

    def get(self, name: str) -> StationRecord:
        stations, _ = self._load()
        try:
            return stations[name]
        except KeyError:
            raise StationError(
                ThreePartMessage(
                    f"There is no station called {name}.",
                    "It has not been added under Admin → Stations.",
                    "Add the station first, then issue its enrolment code.",
                )
            ) from None

    def put(self, record: StationRecord) -> StationRecord:
        with self._lock:
            stations, codes = self._load()
            stations[record.name] = record
            self._save(stations, codes)
        return record

    def remove(self, name: str) -> bool:
        with self._lock:
            stations, codes = self._load()
            existed = stations.pop(name, None) is not None
            codes.pop(name, None)
            self._save(stations, codes)
        return existed

    def code_for(self, name: str) -> EnrolmentCode | None:
        _, codes = self._load()
        return codes.get(name)

    def put_code(self, code: EnrolmentCode | None, name: str) -> None:
        with self._lock:
            stations, codes = self._load()
            if code is None:
                codes.pop(name, None)
            else:
                codes[name] = code
            self._save(stations, codes)


# --- the certificate authority ------------------------------------------------------------------


class IssuedCert(SlasModel):
    cert_pem: str
    key_pem: str
    fingerprint: str


class CertificateAuthority(Protocol):
    def ca_pem(self) -> str: ...

    def issue_cert(
        self, common_name: str, *, san_hosts: Sequence[str] = (), days: int = 397
    ) -> IssuedCert: ...


def san_entries(hosts: Sequence[str]) -> str:
    """`subjectAltName=` for the hosts a certificate must be valid for (IP or DNS)."""
    entries: list[str] = []
    for host in hosts:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            entries.append(f"DNS:{host}")
        else:
            entries.append(f"IP:{host}")
    return ",".join(dict.fromkeys(entries))


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or ""


class OpensslCa:
    """The platform CA as files, driven through the `openssl` binary (argv only). The CA key
    never leaves this class; each issued key is generated on the platform and handed to the
    station exactly once, then shredded here."""

    def __init__(
        self, *, ca_cert: Path, ca_key: Path, runner: ProcessRunner, workdir: Path
    ) -> None:
        self.ca_cert = ca_cert
        self.ca_key = ca_key
        self.runner = runner
        self.workdir = workdir

    def ca_pem(self) -> str:
        return self.ca_cert.read_text(encoding="utf-8")

    def ensure_ca(self, common_name: str = "slas-factory-ca", *, days: int = 3650) -> bool:
        """Create the CA at first start when it does not exist yet (nothing to set up by hand,
        INV-10). Returns True when it was created now."""
        if self.ca_cert.is_file() and self.ca_key.is_file():
            return False
        self.ca_cert.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._run(
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(self.ca_key),
            "-out",
            str(self.ca_cert),
            "-days",
            str(days),
            "-subj",
            f"/CN={common_name}",
        )
        self.ca_key.chmod(0o600)
        return True

    def _run(self, *argv: str) -> None:
        result = self.runner.run(["openssl", *argv], env={}, timeout_s=60)
        if result.exit_code != 0:
            raise StationError(
                ThreePartMessage(
                    "The platform CA could not issue a certificate.",
                    (result.stderr.strip().splitlines() or [f"openssl exited {result.exit_code}"])[
                        -1
                    ],
                    "Check that openssl is installed in the factory executor and that the CA "
                    "files under Factory/ca are readable.",
                )
            )

    def issue_cert(
        self, common_name: str, *, san_hosts: Sequence[str] = (), days: int = 397
    ) -> IssuedCert:
        """One certificate for both directions: the station presents it as a server to the
        executor (so it needs the runner host in its SAN) and as a client on enrolment-style
        calls; the executor's own certificate is issued the same way."""
        self.workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        nonce = secrets.token_hex(6)
        key = self.workdir / f"{common_name}-{nonce}.key"
        csr = self.workdir / f"{common_name}-{nonce}.csr"
        cert = self.workdir / f"{common_name}-{nonce}.pem"
        ext = self.workdir / f"{common_name}-{nonce}.ext"
        try:
            san = san_entries([*san_hosts, common_name])
            ext.write_text(
                "extendedKeyUsage=serverAuth,clientAuth\nbasicConstraints=CA:FALSE\n"
                f"subjectAltName={san}\n",
                encoding="utf-8",
            )
            self._run(
                "req",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(csr),
                "-subj",
                f"/CN={common_name}",
            )
            self._run(
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(self.ca_cert),
                "-CAkey",
                str(self.ca_key),
                "-CAcreateserial",
                "-out",
                str(cert),
                "-days",
                str(days),
                "-extfile",
                str(ext),
            )
            result = self.runner.run(
                ["openssl", "x509", "-in", str(cert), "-noout", "-fingerprint", "-sha256"],
                env={},
                timeout_s=60,
            )
            fingerprint = (
                result.stdout.strip().split("=", 1)[-1].strip() if result.exit_code == 0 else ""
            )
            return IssuedCert(
                cert_pem=cert.read_text(encoding="utf-8"),
                key_pem=key.read_text(encoding="utf-8"),
                fingerprint=f"SHA256:{fingerprint}" if fingerprint else "SHA256:unknown",
            )
        finally:
            for path in (key, csr, cert, ext):
                if path.exists():
                    size = path.stat().st_size
                    with path.open("r+b") as handle:
                        handle.write(b"\0" * size)
                    path.unlink()


class FakeCa:
    def __init__(self) -> None:
        self.issued: list[str] = []
        self.sans: list[str] = []

    def ca_pem(self) -> str:
        return "-----BEGIN CERTIFICATE-----\nFAKE-CA\n-----END CERTIFICATE-----\n"

    def issue_cert(
        self, common_name: str, *, san_hosts: Sequence[str] = (), days: int = 397
    ) -> IssuedCert:
        self.issued.append(common_name)
        self.sans.append(san_entries([*san_hosts, common_name]))
        n = len(self.issued)
        cert = (
            f"-----BEGIN CERTIFICATE-----\nFAKE-CERT-{common_name}-{n}\n-----END CERTIFICATE-----\n"
        )
        key = (
            "-----BEGIN EC PRIVATE KEY-----\n"
            f"FAKE-KEY-{common_name}-{n}\n-----END EC PRIVATE KEY-----\n"
        )
        return IssuedCert(
            cert_pem=cert,
            key_pem=key,
            fingerprint="SHA256:" + hashlib.sha256(cert.encode()).hexdigest()[:32].upper(),
        )


# --- the service ----------------------------------------------------------------------------------


def new_code() -> str:
    groups = ["".join(secrets.choice(CODE_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "-".join(groups)


def normalise_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def hash_code(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{normalise_code(code)}".encode()).hexdigest()


class EnrolmentService:
    def __init__(
        self,
        registry: StationRegistry,
        *,
        ca: CertificateAuthority,
        clock: Clock,
        data_root: Path,
        code_ttl_minutes: int = 15,
        max_attempts: int = 5,
    ) -> None:
        self.registry = registry
        self.ca = ca
        self.clock = clock
        self.data_root = data_root
        self.code_ttl = timedelta(minutes=code_ttl_minutes)
        self.max_attempts = max_attempts

    def issue(self, station: str, *, by: str) -> IssuedCode:
        self.registry.get(station)  # must exist
        code = new_code()
        salt = secrets.token_hex(8)
        now = self.clock.now()
        expires = now + self.code_ttl
        self.registry.put_code(
            EnrolmentCode(
                station=station,
                salt=salt,
                code_hash=hash_code(salt, code),
                issued_at=now,
                expires_at=expires,
                issued_by=by,
            ),
            station,
        )
        minutes = int(self.code_ttl.total_seconds() // 60)
        return IssuedCode(
            station=station,
            code=code,
            expires_at=expires,
            sentence=(
                f"Enter this code on {station} within {minutes} minutes: {code}. It works once; "
                "issuing a new code cancels it."
            ),
        )

    def redeem(self, request: EnrolmentRequest) -> EnrolmentGrant:
        now = self.clock.now()
        record = self.registry.get(request.station)
        pending = self.registry.code_for(request.station)
        if pending is None or pending.used_at is not None:
            raise StationError(
                ThreePartMessage(
                    f"No enrolment code is open for {request.station}.",
                    "None was issued, or the last one was already used.",
                    "Issue a new code under Admin → Stations and enter it on the station.",
                )
            )
        if now > pending.expires_at:
            self.registry.put_code(None, request.station)
            raise StationError(
                ThreePartMessage(
                    f"The enrolment code for {request.station} expired at "
                    f"{pending.expires_at:%H:%M}.",
                    "Codes are valid for a few minutes so a code written down cannot be used "
                    "later.",
                    "Issue a new code under Admin → Stations.",
                )
            )
        if pending.attempts >= self.max_attempts:
            raise StationError(
                ThreePartMessage(
                    f"Enrolment of {request.station} is locked after {pending.attempts} "
                    "wrong codes.",
                    "Somebody entered wrong codes too often.",
                    "Issue a new code under Admin → Stations; that unlocks the station.",
                )
            )
        if not hmac.compare_digest(hash_code(pending.salt, request.code), pending.code_hash):
            pending = pending.model_copy(update={"attempts": pending.attempts + 1})
            self.registry.put_code(pending, request.station)
            left = self.max_attempts - pending.attempts
            raise StationError(
                ThreePartMessage(
                    f"The code for {request.station} is not right.",
                    f"{left} {'attempt' if left == 1 else 'attempts'} left before enrolment locks.",
                    "Check the code with the administrator who issued it and try again.",
                )
            )
        issued = self.ca.issue_cert(request.station, san_hosts=[host_of(request.runner_url)])
        batch_key = secrets.token_hex(32)
        key_id = f"{request.station}-{now:%Y%m%d%H%M%S}"
        key_path = self.data_root / "Factory" / "keys" / f"{request.station}.key"
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_atomic(key_path, batch_key + "\n", mode=0o600)
        record = record.model_copy(
            update={
                "runner_url": request.runner_url,
                "enrolled_at": now,
                "cert_fingerprint": issued.fingerprint,
                "batch_key_id": key_id,
            }
        )
        self.registry.put(record)
        self.registry.put_code(pending.model_copy(update={"used_at": now}), request.station)
        return EnrolmentGrant(
            station=request.station,
            ca_pem=self.ca.ca_pem(),
            client_cert_pem=issued.cert_pem,
            client_key_pem=issued.key_pem,
            cert_fingerprint=issued.fingerprint,
            batch_key_id=key_id,
            batch_key=batch_key,
            config=record.config().model_dump(mode="json"),
            sentence=(
                f"{request.station} is enrolled: certificate {issued.fingerprint}, batches signed "
                f"with key {key_id}. The executor will reach it at {request.runner_url}."
            ),
        )

    def revoke(self, station: str) -> StationRecord:
        record = self.registry.get(station).model_copy(
            update={
                "runner_url": None,
                "enrolled_at": None,
                "cert_fingerprint": None,
                "batch_key_id": None,
            }
        )
        self.registry.put(record)
        self.registry.put_code(None, station)
        key_path = self.data_root / "Factory" / "keys" / f"{station}.key"
        if key_path.exists():
            key_path.unlink()
        return record

    def batch_key_ref(self, station: str) -> str:
        return f"file:Factory/keys/{station}.key"


# --- the endpoint the station calls --------------------------------------------------------------


class _EnrolHandler(BaseHTTPRequestHandler):
    service: EnrolmentService
    server_version = "slas-enrolment"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/enrol":
            self._json(
                404,
                ThreePartMessage(
                    "Not found.", f"{self.path} is not an enrolment endpoint.", "POST to /enrol."
                ).as_dict(),
            )
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 64 * 1024:
            self._json(
                413,
                ThreePartMessage(
                    "The request is empty or too large.",
                    "An enrolment request is a few hundred bytes.",
                    "Send station, code and runner_url as JSON.",
                ).as_dict(),
            )
            return
        try:
            request = EnrolmentRequest.model_validate_json(self.rfile.read(length))
        except ValueError as exc:
            self._json(
                400,
                ThreePartMessage(
                    "The enrolment request could not be read.",
                    str(exc)[:300],
                    "Send station, code and runner_url as JSON.",
                ).as_dict(),
            )
            return
        try:
            grant = self.service.redeem(request)
        except StationError as exc:
            self._json(403, exc.message.as_dict())
            return
        self._json(200, grant.model_dump(mode="json"))


class EnrolmentServer:
    """TLS with the executor's server certificate only: the station has no certificate yet."""

    def __init__(
        self, service: EnrolmentService, *, bind: str, certfile: str, keyfile: str
    ) -> None:
        import ssl

        host, _, port = bind.rpartition(":")
        handler = type("EnrolHandler", (_EnrolHandler,), {"service": service})
        self._server = ThreadingHTTPServer((host or "0.0.0.0", int(port)), handler)  # noqa: S104
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def copy_ca_for_bundle(ca_cert: Path, bundle_dir: Path) -> Path:
    """The station bundle carries the platform CA so `enrol` can verify the executor."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    target = bundle_dir / "slas-ca.pem"
    shutil.copyfile(ca_cert, target)
    return target
