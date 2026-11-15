# Notices

This distribution contains code under more than one license.

## libde265-web

The TypeScript sources, JavaScript output, build tooling, declarations, and
`wasm/glue.cpp` are licensed under the MIT License in `LICENSE`.

## libde265

The WebAssembly decoder contains libde265, copyright Struktur AG, Dirk Farin,
and other libde265 contributors. libde265 is licensed under
LGPL-3.0-or-later. Its combined GNU GPLv3/LGPLv3 license text is distributed
in `third_party/libde265/COPYING`, and its authors list is in
`third_party/libde265/AUTHORS`.

The linked version is libde265 v1.1.1, commit
`4dd701fffac01632ffd5cabc5ef10deb56accba1`. The corresponding libde265 source,
application glue, configuration headers, and build script are included in the
npm package so recipients can rebuild and relink the WebAssembly module with a
modified compatible library. The canonical upstream source is
<https://github.com/strukturag/libde265>.

No term of the MIT-licensed application code is intended to restrict
modification of libde265 or reverse engineering for debugging such
modifications.

## HEVC patents

Open-source copyright licenses do not grant patent rights that may be required
to implement or distribute HEVC/H.265. Distributors are responsible for their
own patent review in each applicable jurisdiction and product context.
