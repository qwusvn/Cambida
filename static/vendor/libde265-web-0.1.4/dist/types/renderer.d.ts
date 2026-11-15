import type { UnifiedVideoFrame } from "./frame.js";
import type { ActiveRenderer, RendererMode } from "./types.js";
export type RenderCanvas = HTMLCanvasElement | OffscreenCanvas;
export interface FrameRenderer {
    readonly kind: ActiveRenderer;
    render(frame: UnifiedVideoFrame): void;
    close(): void;
}
type RenderContext = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D;
export declare class CanvasRenderer implements FrameRenderer {
    #private;
    readonly kind: "canvas2d";
    readonly canvas: RenderCanvas;
    readonly context: RenderContext;
    private imageData;
    constructor(canvas: RenderCanvas);
    render(frame: UnifiedVideoFrame): void;
    close(): void;
}
export declare class WebGLRenderer implements FrameRenderer {
    #private;
    readonly kind: "webgl2";
    readonly canvas: RenderCanvas;
    readonly gl: WebGL2RenderingContext;
    private readonly programs;
    private readonly textures;
    private readonly vertexBuffer;
    private closed;
    constructor(canvas: RenderCanvas);
    render(frame: UnifiedVideoFrame): void;
    close(): void;
}
export declare function createRenderer(canvas: RenderCanvas, mode?: RendererMode): FrameRenderer;
export {};
//# sourceMappingURL=renderer.d.ts.map