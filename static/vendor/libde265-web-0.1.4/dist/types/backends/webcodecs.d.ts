import type { BackendOptions, DecoderBackend, HevcAccessUnit } from "../types.js";
type VideoFrameGeometry = Pick<VideoFrame, "codedWidth" | "codedHeight" | "displayWidth" | "displayHeight" | "visibleRect">;
export declare function needsWebCodecsGeometryCorrection(frame: VideoFrameGeometry, expectedDisplayWidth?: number, expectedDisplayHeight?: number): boolean;
export declare class WebCodecsBackend implements DecoderBackend {
    readonly options: BackendOptions;
    private decoder;
    private config;
    private closed;
    constructor(options: BackendOptions);
    get decodeQueueSize(): number;
    static isSupported(config: VideoDecoderConfig): Promise<boolean>;
    init(): Promise<void>;
    decode(accessUnit: HevcAccessUnit, timestamp: number, duration?: number): void;
    flush(): Promise<void>;
    reset(): void;
    close(): void;
}
export {};
//# sourceMappingURL=webcodecs.d.ts.map