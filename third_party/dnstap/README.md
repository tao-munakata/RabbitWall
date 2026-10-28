# dnstap schema

The unmodified dnstap Protocol Buffers schema is provided by the dnstap contributors under CC0, as stated in its header.

- Source: https://github.com/dnstap/dnstap.pb/blob/master/dnstap.proto
- SHA-256: 967cbf7c9d53e33cf70f333f34cac3a1390534eb59c0139a2d77a85a0cfd5606
- Terms: https://creativecommons.org/publicdomain/zero/1.0/

The generated binding is at src/rabbitwall/dnstap_pb2.py. Rebuild from the repository root using the OS protobuf compiler:

    protoc -I third_party/dnstap --python_out=src/rabbitwall third_party/dnstap/dnstap.proto

Generated with protoc 3.21.12 for compatibility with the supported OS packages. Protocol Buffers and Unbound are installed separately; their binaries are not included in the source archive. Python protobuf uses the BSD 3-Clause license; Unbound uses the BSD license. Keep OS security updates installed.

The pip requirement uses protobuf >=6.33.5,<8. Supported OS packages can use an older upstream version with distribution security patches; keep those packages updated. The Debian 13 integration test uses 3.21.12-11+deb13u1, including backported fixes. See the [Debian security tracker](https://security-tracker.debian.org/tracker/source-package/protobuf) and [upstream advisory](https://github.com/advisories/GHSA-7gcm-g887-7qv7).
