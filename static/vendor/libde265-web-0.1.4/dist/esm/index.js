export { HevcPlayer } from "./player.js";
export { AnnexBAccessUnitParser, hevcNalType, isHevcKeyAccessUnit } from "./annexb.js";
export { UnifiedVideoFrame } from "./frame.js";
export { CanvasRenderer, WebGLRenderer, createRenderer } from "./renderer.js";
export { HevcPlayerError } from "./errors.js";
export async function createHevcPlayer(options) {
    const { HevcPlayer } = await import("./player.js");
    const player = new HevcPlayer(options);
    await player.init();
    return player;
}
//# sourceMappingURL=index.js.map