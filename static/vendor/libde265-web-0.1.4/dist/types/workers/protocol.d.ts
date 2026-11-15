import type { ActiveRenderer, RendererMode, WasmVariant, WorkerFrameOutput } from "../types.js";
export interface SerializedError {
    code: string;
    message: string;
}
export interface WorkerInitOptions {
    codec: string;
    frameDuration: number;
    wasmBaseUrl?: string;
    wasmVariant?: WasmVariant;
    threads?: number;
    renderer: RendererMode;
    frameOutput: WorkerFrameOutput;
}
export interface SerializedPlane {
    data: ArrayBuffer;
    stride: number;
    width: number;
    height: number;
    bitDepth: number;
}
export interface SerializedFrame {
    timestamp: number;
    duration?: number;
    codedWidth: number;
    codedHeight: number;
    displayWidth: number;
    displayHeight: number;
    planes?: {
        y: SerializedPlane;
        u: SerializedPlane;
        v: SerializedPlane;
        fullRange: boolean;
        matrixCoefficients: number;
    };
}
export type WasmWorkerRequest = {
    type: "init";
    id: number;
    options: WorkerInitOptions;
} | {
    type: "attach-canvas";
    id: number;
    canvas: OffscreenCanvas;
    renderer: RendererMode;
} | {
    type: "decode";
    data: ArrayBuffer;
    key: boolean;
    timestamp: number;
    duration?: number;
} | {
    type: "flush";
    id: number;
} | {
    type: "reset";
} | {
    type: "close";
};
export type WasmWorkerResponse = {
    type: "response";
    id: number;
    ok: true;
    rendererKind?: ActiveRenderer;
} | {
    type: "response";
    id: number;
    ok: false;
    error: SerializedError;
} | {
    type: "frame";
    frame: SerializedFrame;
} | {
    type: "dequeue";
} | {
    type: "error";
    error: SerializedError;
};
//# sourceMappingURL=protocol.d.ts.map