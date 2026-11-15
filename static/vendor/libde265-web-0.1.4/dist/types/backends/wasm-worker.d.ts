import type { ActiveRenderer, BackendOptions, DecoderBackend, HevcAccessUnit } from "../types.js";
export declare class WasmWorkerBackend implements DecoderBackend {
    #private;
    readonly execution: "worker";
    readonly options: BackendOptions;
    handlesRendering: boolean;
    canvasTransferred: boolean;
    rendererKind: ActiveRenderer | null;
    private worker;
    private pending;
    private nextRequestId;
    private queueSize;
    private closed;
    private initialized;
    constructor(options: BackendOptions);
    static isSupported(): boolean;
    get decodeQueueSize(): number;
    init(): Promise<void>;
    decode(accessUnit: HevcAccessUnit, timestamp: number, duration?: number): void;
    flush(): Promise<void>;
    reset(): void;
    close(): void;
}
//# sourceMappingURL=wasm-worker.d.ts.map