import { WasmBackend } from "../backends/wasm.js";
import { createRenderer } from "../renderer.js";
const scope = globalThis;
let decoder = null;
let renderer = null;
let initOptions = null;
function serializeError(error) {
    const candidate = error;
    return {
        code: typeof candidate?.code === "string" ? candidate.code : "WORKER",
        message: typeof candidate?.message === "string"
            ? candidate.message
            : String(error)
    };
}
function respond(id, action) {
    void Promise.resolve().then(action).then(() => {
        scope.postMessage({ type: "response", id, ok: true });
    }).catch((error) => {
        scope.postMessage({
            type: "response",
            id,
            ok: false,
            error: serializeError(error)
        });
    });
}
function postFrame(frame) {
    const serialized = {
        timestamp: frame.timestamp,
        codedWidth: frame.codedWidth,
        codedHeight: frame.codedHeight,
        displayWidth: frame.displayWidth,
        displayHeight: frame.displayHeight
    };
    if (frame.duration !== undefined)
        serialized.duration = frame.duration;
    const transfer = [];
    if (frame._planes) {
        const { y, u, v } = frame._planes;
        const yBuffer = y.buffer;
        const uBuffer = u.buffer;
        const vBuffer = v.buffer;
        serialized.planes = {
            y: {
                data: yBuffer,
                stride: frame._planes.yStride,
                width: frame._planes.yWidth,
                height: frame._planes.yHeight,
                bitDepth: frame._planes.yBitDepth
            },
            u: {
                data: uBuffer,
                stride: frame._planes.uStride,
                width: frame._planes.uWidth,
                height: frame._planes.uHeight,
                bitDepth: frame._planes.uBitDepth
            },
            v: {
                data: vBuffer,
                stride: frame._planes.vStride,
                width: frame._planes.vWidth,
                height: frame._planes.vHeight,
                bitDepth: frame._planes.vBitDepth
            },
            fullRange: frame._planes.fullRange ?? false,
            matrixCoefficients: frame._planes.matrixCoefficients ?? 2
        };
        transfer.push(yBuffer, uBuffer, vBuffer);
    }
    scope.postMessage({ type: "frame", frame: serialized }, transfer);
    frame.close();
}
async function initialize(options) {
    initOptions = options;
    const backendOptions = {
        mode: "wasm",
        codec: options.codec,
        frameDuration: options.frameDuration,
        render: true,
        wasmWorker: "disabled",
        renderer: options.renderer,
        workerFrameOutput: options.frameOutput,
        lowLatency: {
            enabled: true,
            maxInputFrames: 12,
            maxInputBytes: 8 * 1024 * 1024,
            maxDecodeQueue: 4,
            maxQueueAgeMs: 250,
            dropPolicy: "until-key"
        },
        ...(options.wasmBaseUrl === undefined ? {} : { wasmBaseUrl: options.wasmBaseUrl }),
        ...(options.wasmVariant === undefined ? {} : { wasmVariant: options.wasmVariant }),
        ...(options.threads === undefined ? {} : { threads: options.threads }),
        onFrame: postFrame,
        onError: (error) => scope.postMessage({
            type: "error",
            error: serializeError(error)
        }),
        onDequeue: () => undefined
    };
    decoder = new WasmBackend(backendOptions, {
        onFrameView: (frame) => renderer?.render(frame),
        shouldCopyFrames: () => !(renderer && initOptions?.frameOutput === "metadata")
    });
    await decoder.init();
}
scope.addEventListener("message", (event) => {
    const message = event.data;
    switch (message.type) {
        case "init":
            respond(message.id, () => initialize(message.options));
            break;
        case "attach-canvas":
            void Promise.resolve().then(() => {
                renderer = createRenderer(message.canvas, message.renderer);
                scope.postMessage({
                    type: "response",
                    id: message.id,
                    ok: true,
                    rendererKind: renderer.kind
                });
            }).catch((error) => {
                scope.postMessage({
                    type: "response",
                    id: message.id,
                    ok: false,
                    error: serializeError(error)
                });
            });
            break;
        case "decode":
            try {
                decoder?.decode({ data: new Uint8Array(message.data), key: message.key }, message.timestamp);
            }
            catch (error) {
                scope.postMessage({ type: "error", error: serializeError(error) });
            }
            finally {
                scope.postMessage({ type: "dequeue" });
            }
            break;
        case "flush":
            respond(message.id, async () => decoder?.flush());
            break;
        case "reset":
            decoder?.reset();
            break;
        case "close":
            renderer?.close();
            decoder?.close();
            renderer = null;
            decoder = null;
            scope.close();
            break;
    }
});
//# sourceMappingURL=wasm-worker.js.map