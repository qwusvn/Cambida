# Licensing

English | [简体中文](zh-CN/licensing.md)

The JavaScript and TypeScript sources, declarations, build scripts, and
`wasm/glue.cpp` authored for this repository are licensed under the MIT License
in the repository root.

`third_party/libde265` is a separate Git submodule pinned to libde265 v1.1.1.
libde265 is licensed under LGPL-3.0-or-later; its source notices and combined
GNU GPLv3/LGPLv3 text are authoritative.

Distributions containing the linked libde265 WebAssembly modules must comply
with the LGPL. This includes prominent attribution, providing the required
license texts, allowing reverse engineering for debugging library changes, and
providing a practical way to rebuild or relink against a modified compatible
libde265. The npm package includes the pinned library source, application glue,
configuration headers, and build script for that purpose. See `NOTICE.md` for
the exact version and locations.

HEVC/H.265 may also be covered by patents. An open-source copyright license is
not a patent license. Assess obligations for each jurisdiction, device, service,
content type, and commercial model before distribution. This document is not
legal advice.
