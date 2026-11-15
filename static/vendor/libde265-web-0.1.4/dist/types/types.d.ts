import type { UnifiedVideoFrame } from "./frame.js";
import type { HevcPlayerError } from "./errors.js";
export type DecoderMode = "auto" | "webcodecs" | "wasm";
export type ActiveBackend = "webcodecs" | "wasm";
export type WasmVariant = "auto" | "baseline" | "simd" | "threads-simd";
export type WasmWorkerMode = "auto" | "disabled" | "required";
export type WorkerFrameOutput = "planes" | "metadata";
export type RendererMode = "auto" | "canvas2d" | "webgl";
export type ActiveRenderer = "canvas2d" | "webgl2";
export type WasmExecution = "main" | "worker";
export type PlayerState = "idle" | "initializing" | "ready" | "error" | "closed";
export type BinaryData = ArrayBuffer | ArrayBufferView;
export type DropPolicy = "none" | "until-key";
export type BackpressureLevel = "normal" | "high";
export type DropStage = "input" | "render";
export type DropReason = "input-overflow" | "queue-age" | "waiting-key" | "render-backpressure";
export interface HevcAccessUnit {
    data: Uint8Array;
    key: boolean;
}
export interface FrameMetadata {
    /** Timestamp in microseconds. */
    timestamp?: number;
    /** Duration in microseconds. */
    duration?: number;
}
export interface AccessUnitMetadata extends FrameMetadata {
    /** Set explicitly when the custom protocol already knows this is an IRAP frame. */
    key?: boolean;
    /** Optional monotonically increasing identifier from the transport protocol. */
    sequence?: number;
}
export interface LowLatencyOptions {
    /** Enables bounded queues and latency recovery. Defaults to true. */
    enabled?: boolean;
    /** Maximum encoded Access Units waiting in the player. */
    maxInputFrames?: number;
    /** Maximum encoded bytes waiting in the player. */
    maxInputBytes?: number;
    /** Maximum WebCodecs control-queue depth. */
    maxDecodeQueue?: number;
    /** Maximum age of the oldest queued Access Unit. */
    maxQueueAgeMs?: number;
    /**
     * "until-key" resets the decoder and discards input until the next IRAP
     * Access Unit. "none" reports pressure but never drops encoded input.
     */
    dropPolicy?: DropPolicy;
}
export interface NormalizedLowLatencyOptions {
    enabled: boolean;
    maxInputFrames: number;
    maxInputBytes: number;
    maxDecodeQueue: number;
    maxQueueAgeMs: number;
    dropPolicy: DropPolicy;
}
export interface PlayerStats {
    backend: ActiveBackend | null;
    renderer: ActiveRenderer | null;
    wasmExecution: WasmExecution | null;
    inputQueueFrames: number;
    inputQueueBytes: number;
    decodeQueueSize: number;
    queueAgeMs: number;
    waitingForKeyframe: boolean;
    decodedFrames: number;
    renderedFrames: number;
    droppedInputFrames: number;
    droppedRenderFrames: number;
}
export interface HevcPlayerOptions {
    canvas?: HTMLCanvasElement | OffscreenCanvas;
    mode?: DecoderMode;
    /** RFC 6381 fully-qualified HEVC codec string. */
    codec?: string;
    codedWidth?: number;
    codedHeight?: number;
    /**
     * Expected display width. When both expected dimensions fit within the
     * decoded coded size, an inconsistent WebCodecs visibleRect is corrected
     * before rendering. This is a data-driven workaround for browser metadata
     * bugs and has no effect on WASM frames or already-correct WebCodecs frames.
     */
    expectedDisplayWidth?: number;
    /**
     * Expected display height. Must be supplied together with
     * expectedDisplayWidth.
     */
    expectedDisplayHeight?: number;
    hardwareAcceleration?: HardwareAcceleration;
    /** Used when the input does not carry timestamps, in microseconds. */
    frameDuration?: number;
    render?: boolean;
    autoCloseFrames?: boolean;
    lowLatency?: boolean | LowLatencyOptions;
    url?: string;
    protocols?: string | string[];
    wasmBaseUrl?: string | URL;
    wasmVariant?: WasmVariant;
    /** Runs the WASM backend in a Dedicated Worker when supported. */
    wasmWorker?: WasmWorkerMode;
    /** Renderer used for WASM I420 frames. WebCodecs keeps its native 2D path. */
    renderer?: RendererMode;
    /**
     * "planes" preserves frame.planes across the worker boundary.
     * "metadata" avoids plane copies when the worker owns the render canvas.
     */
    workerFrameOutput?: WorkerFrameOutput;
    /** Optional override for the emitted module worker URL. */
    workerUrl?: string | URL;
    /** Number of libde265 workers. The bundled threaded build has a pool of four. */
    threads?: number;
    onFrame?: (frame: UnifiedVideoFrame) => void | Promise<void>;
    onError?: (error: HevcPlayerError) => void;
}
export interface PlayerEventMap {
    ready: {
        backend: ActiveBackend;
    };
    fallback: {
        from: "webcodecs";
        to: "wasm";
        error: unknown;
    };
    workerfallback: {
        from: "worker";
        to: "main";
        error: unknown;
    };
    open: {
        url: string;
    };
    close: {
        code: number;
        reason: string;
    };
    frame: UnifiedVideoFrame;
    drop: {
        stage: DropStage;
        reason: DropReason;
        timestamp: number;
        key?: boolean;
        sequence?: number;
    };
    backpressure: {
        level: BackpressureLevel;
        stats: PlayerStats;
    };
    queuechange: PlayerStats;
    keyframerequest: {
        reason: "input-overflow" | "queue-age";
    };
    error: HevcPlayerError;
}
export interface NormalizedPlayerOptions extends Omit<HevcPlayerOptions, "lowLatency"> {
    mode: DecoderMode;
    codec: string;
    frameDuration: number;
    render: boolean;
    wasmWorker: WasmWorkerMode;
    renderer: RendererMode;
    workerFrameOutput: WorkerFrameOutput;
    lowLatency: NormalizedLowLatencyOptions;
}
export interface DecoderBackend {
    readonly decodeQueueSize: number;
    readonly execution?: WasmExecution;
    readonly handlesRendering?: boolean;
    readonly rendererKind?: ActiveRenderer | null;
    init(): Promise<void>;
    decode(accessUnit: HevcAccessUnit, timestamp: number, duration?: number): void;
    flush(): Promise<void>;
    reset(): void;
    close(): void;
}
export interface BackendOptions extends NormalizedPlayerOptions {
    onFrame: (frame: UnifiedVideoFrame) => void;
    onError: (error: HevcPlayerError) => void;
    onDequeue: () => void;
}
//# sourceMappingURL=types.d.ts.map