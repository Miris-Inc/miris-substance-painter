# Miris for Adobe Substance 3D Painter

Installers for the Miris publish plugin for Adobe Substance 3D Painter. Each
release carries the macOS package (signed and notarized), the Windows
installer, a ZIP of the plugin, and a `release-manifest.json` listing the
SHA-256 digest of every file.

**[Download the latest release](https://github.com/Miris-Inc/miris-substance-painter/releases/latest)**

- macOS: `miris-substance-painter-<version>.pkg`
- Windows: `miris-substance-painter-<version>.exe`

Releases are published by CI from the `integrations` repository; nothing is
uploaded here by hand. Install instructions are in [INSTALL.md](INSTALL.md),
which also ships inside the package.

## Source

The [`miris/`](miris) folder is the plugin's source code as shipped in release
`substance-painter/v0.8.0`: the same files, byte for byte, that are inside
`miris-substance-painter-0.8.0.zip` and that the installers put into Painter's
plugin folder. It was taken from the Miris integrations repository at commit
`173ed4685db2d1dd6250ec7964c1e4124408fb4e`.

This repository is a read-only snapshot. Development happens internally, and
issues and pull requests are not accepted here.

## Support

Found a bug or need help? Email [support@miris.com](mailto:support@miris.com).

## License

Apache 2.0. See [LICENSE](LICENSE).
