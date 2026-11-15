import { UnifiedVideoFrame } from "../frame.js";
import { HevcPlayerError } from "../errors.js";
function supportsSimd() {
    return typeof WebAssembly === "object" && WebAssembly.validate(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0, 1, 5, 1, 96, 0, 1, 123, 3, 2, 1, 0, 10, 10, 1, 8, 0, 65, 0, 253, 15, 253, 98, 11]));
}
function selectVariant(requested) {
    if (requested && requested !== "auto")
        return requested;
    return supportsSimd() ? "simd" : "baseline";
}
export function getWasmVariantCandidates(requested, selected) {
    if (requested && requested !== "auto") {
        return [requested];
    }
    return selected === "simd" ? ["simd", "baseline"] : ["baseline"];
}
export class WasmBackend {
    options;
    execution = "main";
    hooks;
    module = null;
    context = 0;
    closed = false;
    variant;
    constructor(options, hooks = {}) {
        this.options = options;
        this.hooks = hooks;
        this.variant = selectVariant(options.wasmVariant);
    }
    get decodeQueueSize() {
        return 0;
    }
    async init() {
        const configuredBase = this.options.wasmBaseUrl
            ?? new URL("../../wasm/", import.meta.url);
        const baseUrl = new URL(configuredBase, globalThis.document?.baseURI ?? import.meta.url).href;
        const candidates = getWasmVariantCandidates(this.options.wasmVariant, this.variant);
        let lastError;
        let selectedVariant = this.variant;
        for (const candidate of candidates) {
            const moduleUrl = new URL(`libde265-${candidate}.mjs`, baseUrl);
            try {
                const imported = await import(moduleUrl.href);
                const createModule = imported.default;
                this.module = await createModule({
                    locateFile: (file) => new URL(file, baseUrl).href
                });
                selectedVariant = candidate;
                break;
            }
            catch (error) {
                const detail = error instanceof Error && error.message
                    ? ` ${error.message}`
                    : "";
                lastError = new HevcPlayerError("WASM_LOAD", `Unable to initialize ${moduleUrl.href}.${detail}`, error);
            }
        }
        if (!this.module) {
            throw lastError ?? new HevcPlayerError("WASM_LOAD", "No WASM variant could be loaded.");
        }
        this.variant = selectedVariant;
        const threads = selectedVariant === "threads-simd"
            ? Math.min(4, Math.max(1, this.options.threads
                ?? Math.min(4, globalThis.navigator?.hardwareConcurrency ?? 2)))
            : 0;
        this.context = this.module._hevc_decoder_create(threads);
        if (!this.context)
            throw new HevcPlayerError("WASM_INIT", "libde265 decoder creation failed.");
    }
    decode(accessUnit, timestamp) {
        const module = this.module;
        if (!module || !this.context || this.closed) {
            throw new HevcPlayerError("DECODER_CLOSED", "WASM decoder is not open.");
        }
        const data = accessUnit.data;
        const pointer = module._malloc(data.byteLength);
        if (!pointer)
            throw new HevcPlayerError("WASM_MEMORY", "WASM memory allocation failed.");
        try {
            module.HEAPU8.set(data, pointer);
            const result = module._hevc_decoder_push(this.context, pointer, data.byteLength, timestamp);
            if (result !== 0) {
                throw new HevcPlayerError("WASM_DECODE", `libde265 returned error ${result}.`);
            }
            this.#pump();
        }
        finally {
            module._free(pointer);
        }
    }
    #drainFrames() {
        const module = this.module;
        if (!module)
            return 0;
        let count = 0;
        while (module._hevc_decoder_next_frame(this.context)) {
            try {
                const width = module._hevc_frame_width(this.context);
                const height = module._hevc_frame_height(this.context);
                const yStride = module._hevc_frame_stride(this.context, 0);
                const uStride = module._hevc_frame_stride(this.context, 1);
                const vStride = module._hevc_frame_stride(this.context, 2);
                const yWidth = module._hevc_frame_plane_width(this.context, 0);
                const uWidth = module._hevc_frame_plane_width(this.context, 1);
                const vWidth = module._hevc_frame_plane_width(this.context, 2);
                const yHeight = module._hevc_frame_plane_height(this.context, 0);
                const uHeight = module._hevc_frame_plane_height(this.context, 1);
                const vHeight = module._hevc_frame_plane_height(this.context, 2);
                const yBitDepth = module._hevc_frame_bit_depth(this.context, 0);
                const uBitDepth = module._hevc_frame_bit_depth(this.context, 1);
                const vBitDepth = module._hevc_frame_bit_depth(this.context, 2);
                // Older prebuilt WASM bundles do not expose VUI metadata. Preserve
                // compatibility and use normal video defaults until they are rebuilt.
                const fullRange = module._hevc_frame_full_range?.(this.context) === 1;
                const matrixCoefficients = module._hevc_frame_matrix_coefficients?.(this.context) ?? 2;
                const yPtr = module._hevc_frame_plane(this.context, 0);
                const uPtr = module._hevc_frame_plane(this.context, 1);
                const vPtr = module._hevc_frame_plane(this.context, 2);
                const view = (ptr, size) => module.HEAPU8.subarray(ptr, ptr + size);
                const frameInit = {
                    timestamp: module._hevc_frame_timestamp(this.context),
                    codedWidth: width,
                    codedHeight: height,
                    displayWidth: width,
                    displayHeight: height,
                    backend: "wasm"
                };
                const planeMetadata = {
                    yStride,
                    uStride,
                    vStride,
                    yWidth,
                    uWidth,
                    vWidth,
                    yHeight,
                    uHeight,
                    vHeight,
                    yBitDepth,
                    uBitDepth,
                    vBitDepth,
                    fullRange,
                    matrixCoefficients
                };
                const transientFrame = new UnifiedVideoFrame({
                    ...frameInit,
                    planes: {
                        y: view(yPtr, yStride * yHeight),
                        u: view(uPtr, uStride * uHeight),
                        v: view(vPtr, vStride * vHeight),
                        ...planeMetadata
                    }
                });
                try {
                    this.hooks.onFrameView?.(transientFrame);
                    if (this.hooks.shouldCopyFrames?.() === false) {
                        this.options.onFrame(new UnifiedVideoFrame(frameInit));
                    }
                    else {
                        const planes = transientFrame._planes;
                        this.options.onFrame(new UnifiedVideoFrame({
                            ...frameInit,
                            planes: {
                                y: planes.y.slice(),
                                u: planes.u.slice(),
                                v: planes.v.slice(),
                                ...planeMetadata
                            }
                        }));
                    }
                }
                finally {
                    transientFrame.close();
                }
                count += 1;
            }
            finally {
                module._hevc_decoder_release_frame(this.context);
            }
        }
        return count;
    }
    #pump() {
        const module = this.module;
        if (!module)
            return;
        this.#drainFrames();
        for (let iterations = 0; iterations < 10_000; iterations += 1) {
            const result = module._hevc_decoder_decode(this.context);
            this.#drainFrames();
            if (result === 13)
                return;
            if (result !== 0 && result !== 9) {
                throw new HevcPlayerError("WASM_DECODE", `libde265 returned error ${result}.`);
            }
            if (!module._hevc_decoder_has_more(this.context) && result !== 9)
                return;
        }
        throw new HevcPlayerError("WASM_DECODE_LOOP", "libde265 decode loop did not converge.");
    }
    async flush() {
        if (!this.module || !this.context)
            return;
        const result = this.module._hevc_decoder_flush(this.context);
        if (result !== 0) {
            throw new HevcPlayerError("WASM_FLUSH", `libde265 returned error ${result}.`);
        }
        this.#pump();
    }
    reset() {
        if (this.module && this.context)
            this.module._hevc_decoder_reset(this.context);
    }
    close() {
        this.closed = true;
        if (this.module && this.context)
            this.module._hevc_decoder_destroy(this.context);
        this.context = 0;
    }
}
//# sourceMappingURL=wasm.js.map