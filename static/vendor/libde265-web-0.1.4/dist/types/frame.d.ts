import type { ActiveBackend } from "./types.js";
export interface I420Planes {
    y: Uint8Array;
    u: Uint8Array;
    v: Uint8Array;
    yStride: number;
    uStride: number;
    vStride: number;
    yWidth: number;
    uWidth: number;
    vWidth: number;
    yHeight: number;
    uHeight: number;
    vHeight: number;
    yBitDepth: number;
    uBitDepth: number;
    vBitDepth: number;
    /** HEVC VUI video_full_range_flag. Unspecified streams default to limited range. */
    fullRange?: boolean;
    /** HEVC VUI matrix_coeffs value (ITU-T H.273 / ISO/IEC 23091-2). */
    matrixCoefficients?: number;
}
export interface UnifiedVideoFrameInit {
    timestamp: number;
    duration?: number;
    codedWidth: number;
    codedHeight: number;
    displayWidth?: number;
    displayHeight?: number;
    backend: ActiveBackend;
    nativeFrame?: VideoFrame;
    planes?: I420Planes;
}
export declare class UnifiedVideoFrame {
    readonly timestamp: number;
    readonly duration: number | undefined;
    readonly codedWidth: number;
    readonly codedHeight: number;
    readonly displayWidth: number;
    readonly displayHeight: number;
    readonly backend: ActiveBackend;
    _nativeFrame: VideoFrame | undefined;
    _planes: I420Planes | undefined;
    private _closed;
    constructor(init: UnifiedVideoFrameInit);
    get closed(): boolean;
    /** Native WebCodecs frame, available only while a WebCodecs frame is open. */
    get nativeFrame(): VideoFrame | undefined;
    /** Decoded I420 planes, available only while a WASM frame is open. */
    get planes(): Readonly<I420Planes> | undefined;
    close(): void;
}
//# sourceMappingURL=frame.d.ts.map