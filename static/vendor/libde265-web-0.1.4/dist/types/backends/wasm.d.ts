import { UnifiedVideoFrame } from "../frame.js";
import type { BackendOptions, DecoderBackend, HevcAccessUnit, WasmVariant } from "../types.js";
export interface WasmBackendHooks {
    /** Receives a synchronous view into the WASM heap before libde265 releases it. */
    onFrameView?: (frame: UnifiedVideoFrame) => void;
    /** Return false when only frame metadata needs to cross the backend boundary. */
    shouldCopyFrames?: () => boolean;
}
export declare function getWasmVariantCandidates(requested: WasmVariant | undefined, selected: Exclude<WasmVariant, "auto">): Array<Exclude<WasmVariant, "auto">>;
export declare class WasmBackend implements DecoderBackend {
    #private;
    readonly options: BackendOptions;
    readonly execution: "main";
    readonly hooks: WasmBackendHooks;
    private module;
    private context;
    private closed;
    variant: Exclude<WasmVariant, "auto">;
    constructor(options: BackendOptions, hooks?: WasmBackendHooks);
    get decodeQueueSize(): number;
    init(): Promise<void>;
    decode(accessUnit: HevcAccessUnit, timestamp: number): void;
    flush(): Promise<void>;
    reset(): void;
    close(): void;
}
//# sourceMappingURL=wasm.d.ts.map