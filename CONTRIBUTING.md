# Development

Program flow and function index: [English](docs/PROGRAM_FLOW.md) | [日本語](docs/PROGRAM_FLOW.ja.md).

Python 3.11+ (stdlib except dnspython and protobuf), Node.js for map tests, OpenSSL for PKI tests. Prefer a virtual environment for local dependencies.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 -B -m unittest discover -s tests -p 'test_*.py'
node tests/test_atlas.cjs
```

DNS tests use ephemeral loopback UDP/TCP sockets. Linux kernel tests only run in a disposable Docker container with no external network:

```sh
docker build -t rabbitwall-gate-test -f tests/Dockerfile .
docker run --rm --privileged --network none -e RABBITWALL_DISPOSABLE_TEST=1 -v "$PWD:/workspace:ro" -w /workspace rabbitwall-gate-test /usr/bin/python3 -B tests/linux_gate_smoke.py
docker run --rm --privileged --network none -e RABBITWALL_DISPOSABLE_TEST=1 -e RABBITWALL_CONFIG=/workspace/examples/rabbitwall.example.json -v "$PWD:/workspace:ro" -w /workspace rabbitwall-gate-test /usr/bin/python3 -B tests/public_network_smoke.py
```

Never run these namespace tests on the actual gateway. Kernel tests do not validate real hardware or NetworkManager/systemd orchestration; those require a dedicated test machine. Test settings with a second subnet and non-eth NIC names. UI must not require browser secrets, remote GeoIP lookups, or HTML interpolation of observed DNS names.

Build a distributable archive from an explicit source allowlist:

```sh
python3 tools/release.py --output output/rabbitwall.tar.gz
```

The exact file allowlist is `tools/release-files.txt`; new files must be added intentionally. It excludes generated configuration, PKI, machine backups, captured reports and Git history. The archive includes a SHA-256 file.

Runtime code belongs in `src/rabbitwall/`. The root `setup.py` is the setup CLI, not a setuptools distribution. `tools/deploy.py` is its internal complete-application deployment helper; do not add feature-specific installers.

The disposable container can also run `python3 -B tests/deployment_smoke.py` with the same privileged, network-none and disposable-test settings. It validates application file deployment and backups with real nftables; systemd commands are simulated.

The real Unbound/dnstap integration test is `tests/unbound_smoke.py`, run with the same disposable privileged network-none container flags. It checks cached/uncached UDP and TCP DNS, per-client TTL evidence, observer outage/reconnection/backpressure, and the closed gate. The OS Unbound build must include `--enable-dnstap`.

To exercise the supported Ubuntu package versions, build with `docker build --build-arg TEST_BASE=ubuntu:24.04 -t rabbitwall-ubuntu-test -f tests/Dockerfile .` and use that image for the unit and Unbound integration commands. CI exercises both Debian and Ubuntu DNS packages. These tests run real Unbound under a non-root account; service-manager commands in the deployment test remain simulated.
