import { existsSync, mkdirSync } from "node:fs";
import { delimiter, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(fileURLToPath(new URL("..", import.meta.url)));
const libRoot = join(projectRoot, "third_party", "libde265");
const outputDir = join(projectRoot, "dist", "wasm");

const values = new Map();
const switches = new Set();
for (let index = 2; index < process.argv.length; index += 1) {
  const argument = process.argv[index];
  if (argument === "--enable-lto") {
    switches.add(argument);
  } else if (argument?.startsWith("--")) {
    const value = process.argv[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`Missing value for ${argument}`);
    values.set(argument, value);
    index += 1;
  } else {
    throw new Error(`Unknown argument: ${argument}`);
  }
}

const variant = values.get("--variant") ?? "all";
const configuration = values.get("--configuration") ?? "Release";
if (!["all", "baseline", "simd", "threads-simd"].includes(variant)) {
  throw new Error(`Invalid --variant: ${variant}`);
}
if (!["Release", "Debug"].includes(configuration)) {
  throw new Error(`Invalid --configuration: ${configuration}`);
}

function compilerCandidates() {
  const explicit = values.get("--compiler");
  if (explicit) return [explicit];

  const emsdk = values.get("--emsdk") ?? process.env.EMSDK;
  const names = process.platform === "win32"
    ? ["em++.bat", "em++.exe", "em++"]
    : ["em++"];
  const candidates = [];
  if (emsdk) {
    for (const name of names) candidates.push(join(emsdk, "upstream", "emscripten", name));
  }
  for (const directory of (process.env.PATH ?? "").split(delimiter)) {
    if (!directory) continue;
    for (const name of names) candidates.push(join(directory, name));
  }
  return candidates;
}

const compiler = compilerCandidates().find(existsSync);
if (!compiler) {
  throw new Error(
    "Emscripten compiler not found. Activate emsdk, set EMSDK, or pass --compiler <path>."
  );
}
if (!existsSync(join(libRoot, "libde265", "de265.cc"))) {
  throw new Error("libde265 submodule is missing. Run: git submodule update --init --recursive");
}

mkdirSync(outputDir, { recursive: true });

const sourceNames = [
  "alloc_pool.cc", "bitstream.cc", "cabac.cc", "contextmodel.cc", "de265.cc",
  "deblock.cc", "decctx.cc", "dpb.cc", "fallback-dct.cc", "fallback-deblk.cc",
  "fallback-intrapred.cc", "fallback-motion.cc", "fallback.cc", "image-io.cc",
  "image.cc", "intrapred.cc", "md5.cc", "motion.cc", "nal-parser.cc", "nal.cc",
  "pps.cc", "quality.cc", "refpic.cc", "sao.cc", "scan.cc", "sei.cc", "slice.cc",
  "sps.cc", "threads.cc", "transform.cc", "util.cc", "visualize.cc", "vps.cc",
  "vui.cc"
];
const sources = sourceNames.map((name) => join(libRoot, "libde265", name));
const missingSources = sources.filter((source) => !existsSync(source));
if (missingSources.length > 0) {
  throw new Error(
    "Missing sources required by libde265 v1.1.1. The submodule may be checked " +
    "out at an older revision. From a Git checkout, run:\n" +
    "  git -C third_party/libde265 checkout v1.1.1\n\n" +
    missingSources.join("\n")
  );
}

const exports = [
  "_malloc", "_free", "_hevc_decoder_create", "_hevc_decoder_destroy",
  "_hevc_decoder_push", "_hevc_decoder_flush", "_hevc_decoder_decode",
  "_hevc_decoder_has_more", "_hevc_decoder_reset", "_hevc_decoder_next_frame",
  "_hevc_decoder_release_frame", "_hevc_frame_width", "_hevc_frame_height",
  "_hevc_frame_plane_width", "_hevc_frame_plane_height", "_hevc_frame_stride",
  "_hevc_frame_plane", "_hevc_frame_bit_depth", "_hevc_frame_full_range",
  "_hevc_frame_matrix_coefficients", "_hevc_frame_timestamp"
];
const exportArgument = `-sEXPORTED_FUNCTIONS=[${exports.map((name) => `'${name}'`).join(",")}]`;
const optimizationFlags = configuration === "Release"
  ? ["-O3", "-sASSERTIONS=0"]
  : ["-O1", "-gsource-map", "-sASSERTIONS=1"];
if (switches.has("--enable-lto")) optimizationFlags.push("-flto");

function build(name, featureFlags) {
  const output = join(outputDir, `libde265-${name}.mjs`);
  const args = [
    "-std=c++17", ...optimizationFlags,
    "-DHAVE_CONFIG_H", "-DLIBDE265_STATIC_BUILD", "-DDE265_LOG_ERROR",
    `-I${join(projectRoot, "wasm", "include")}`,
    `-I${libRoot}`,
    `-I${join(libRoot, "libde265")}`,
    join(projectRoot, "wasm", "glue.cpp"), ...sources,
    "-o", output, "--no-entry",
    "-sMODULARIZE=1", "-sEXPORT_ES6=1", "-sENVIRONMENT=web,worker,node",
    "-sFILESYSTEM=0", "-sALLOW_MEMORY_GROWTH=1", "-sINITIAL_MEMORY=268435456",
    "-sSTACK_SIZE=2097152", "-sEXPORTED_RUNTIME_METHODS=HEAPU8",
    exportArgument, ...featureFlags
  ];
  console.log(`Building WASM variant: ${name}`);
  const result = spawnSync(compiler, args, {
    cwd: projectRoot,
    stdio: "inherit",
    shell: process.platform === "win32" && /\.(cmd|bat)$/i.test(compiler)
  });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`Emscripten failed for ${name} (${result.status}).`);
}

const variants = variant === "all" ? ["baseline", "simd", "threads-simd"] : [variant];
for (const name of variants) {
  if (name === "baseline") build(name, []);
  if (name === "simd") build(name, ["-msimd128"]);
  if (name === "threads-simd") {
    build(name, [
      "-msimd128", "-pthread", "-sPTHREAD_POOL_SIZE=4",
      "-sPTHREAD_POOL_SIZE_STRICT=0", "-sALLOW_MEMORY_GROWTH=0"
    ]);
  }
}

console.log(`WASM output: ${outputDir}`);
