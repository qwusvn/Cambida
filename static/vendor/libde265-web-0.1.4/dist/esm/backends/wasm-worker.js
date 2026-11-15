import { HevcPlayerError } from "../errors.js";
import { UnifiedVideoFrame } from "../frame.js";
export class WasmWorkerBackend {
    execution = "worker";
    options;
    handlesRendering = false;
    canvasTransferred = false;
    rendererKind = null;
    worker = null;
    pending = new Map();
    nextRequestId = 1;
    queueSize = 0;
    closed = false;
    initialized = false;
    constructor(options) {
        this.options = options;
    }
    static isSupported() {
        return typeof globalThis.Worker === "function";
    }
    get decodeQueueSize() {
        return this.queueSize;
    }
    async init() {
        if (!WasmWorkerBackend.isSupported()) {
            throw new HevcPlayerError("WORKER_UNSUPPORTED", "Dedicated Worker is not available.");
        }
        const workerUrl = this.options.workerUrl
            ? new URL(this.options.workerUrl, globalThis.document?.baseURI ?? import.meta.url)
            : new URL("../workers/wasm-worker.js", import.meta.url);
        const worker = new globalThis.Worker(workerUrl, {
            type: "module",
            name: "libde265-decoder"
        });
        this.worker = worker;
        worker.addEventListener("message", this.#onMessage);
        worker.addEventListener("error", this.#onWorkerError);
        worker.addEventListener("messageerror", this.#onMessageError);
        await this.#request({
            type: "init",
            options: {
                codec: this.options.codec,
                frameDuration: this.options.frameDuration,
                renderer: this.options.renderer,
                frameOutput: this.options.workerFrameOutput,
                ...(this.options.wasmBaseUrl === undefined
                    ? {}
                    : { wasmBaseUrl: new URL(this.options.wasmBaseUrl, globalThis.document?.baseURI ?? import.meta.url).href }),
                ...(this.options.wasmVariant === undefined
                    ? {}
                    : { wasmVariant: this.options.wasmVariant }),
                ...(this.options.threads === undefined ? {} : { threads: this.options.threads })
            }
        });
        if (this.options.canvas && this.options.render) {
            const offscreen = this.#takeOffscreenCanvas(this.options.canvas);
            if (offscreen) {
                this.canvasTransferred = true;
                const response = await this.#request({
                    type: "attach-canvas",
                    canvas: offscreen,
                    renderer: this.options.renderer
                }, [offscreen]);
                this.handlesRendering = true;
                this.rendererKind = response.rendererKind ?? null;
            }
        }
        this.initialized = true;
    }
    decode(accessUnit, timestamp, duration) {
        if (!this.worker || this.closed) {
            throw new HevcPlayerError("DECODER_CLOSED", "WASM worker is not open.");
        }
        const bytes = accessUnit.data.slice();
        this.queueSize += 1;
        const message = {
            type: "decode",
            data: bytes.buffer,
            key: accessUnit.key,
            timestamp,
            ...(duration === undefined ? {} : { duration })
        };
        this.worker.postMessage(message, [bytes.buffer]);
    }
    async flush() {
        if (!this.worker || this.closed)
            return;
        await this.#request({ type: "flush" });
    }
    reset() {
        if (!this.worker || this.closed)
            return;
        this.worker.postMessage({ type: "reset" });
    }
    close() {
        if (this.closed)
            return;
        this.closed = true;
        if (this.worker) {
            this.worker.postMessage({ type: "close" });
            this.worker.removeEventListener("message", this.#onMessage);
            this.worker.removeEventListener("error", this.#onWorkerError);
            this.worker.removeEventListener("messageerror", this.#onMessageError);
            this.worker.terminate();
        }
        this.worker = null;
        this.queueSize = 0;
        this.#rejectPending(new HevcPlayerError("WORKER_CLOSED", "WASM worker was closed."));
    }
    #takeOffscreenCanvas(canvas) {
        if (typeof globalThis.OffscreenCanvas === "function"
            && canvas instanceof globalThis.OffscreenCanvas) {
            return canvas;
        }
        const transferable = canvas;
        return typeof transferable.transferControlToOffscreen === "function"
            ? transferable.transferControlToOffscreen()
            : null;
    }
    #request(message, transfer = []) {
        if (!this.worker || this.closed) {
            return Promise.reject(new HevcPlayerError("WORKER_CLOSED", "WASM worker is not available."));
        }
        const id = this.nextRequestId++;
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                this.pending.delete(id);
                reject(new HevcPlayerError("WORKER_TIMEOUT", `WASM worker request "${message.type}" timed out.`));
            }, 30_000);
            this.pending.set(id, { resolve, reject, timer });
            this.worker.postMessage({ ...message, id }, transfer);
        });
    }
    #onMessage = (event) => {
        const message = event.data;
        if (message.type === "response") {
            const pending = this.pending.get(message.id);
            if (!pending)
                return;
            this.pending.delete(message.id);
            clearTimeout(pending.timer);
            if (message.ok)
                pending.resolve(message);
            else
                pending.reject(this.#toError(message.error));
            return;
        }
        if (message.type === "dequeue") {
            this.queueSize = Math.max(0, this.queueSize - 1);
            this.options.onDequeue();
            return;
        }
        if (message.type === "error") {
            this.options.onError(this.#toError(message.error));
            return;
        }
        const data = message.frame;
        this.options.onFrame(new UnifiedVideoFrame({
            timestamp: data.timestamp,
            ...(data.duration === undefined ? {} : { duration: data.duration }),
            codedWidth: data.codedWidth,
            codedHeight: data.codedHeight,
            displayWidth: data.displayWidth,
            displayHeight: data.displayHeight,
            backend: "wasm",
            ...(data.planes
                ? {
                    planes: {
                        y: new Uint8Array(data.planes.y.data),
                        u: new Uint8Array(data.planes.u.data),
                        v: new Uint8Array(data.planes.v.data),
                        yStride: data.planes.y.stride,
                        uStride: data.planes.u.stride,
                        vStride: data.planes.v.stride,
                        yWidth: data.planes.y.width,
                        uWidth: data.planes.u.width,
                        vWidth: data.planes.v.width,
                        yHeight: data.planes.y.height,
                        uHeight: data.planes.u.height,
                        vHeight: data.planes.v.height,
                        yBitDepth: data.planes.y.bitDepth,
                        uBitDepth: data.planes.u.bitDepth,
                        vBitDepth: data.planes.v.bitDepth,
                        fullRange: data.planes.fullRange,
                        matrixCoefficients: data.planes.matrixCoefficients
                    }
                }
                : {})
        }));
    };
    #onWorkerError = (event) => {
        const error = new HevcPlayerError("WORKER_RUNTIME", event.message || "WASM worker failed.", event.error);
        this.#rejectPending(error);
        if (this.initialized)
            this.options.onError(error);
    };
    #onMessageError = () => {
        const error = new HevcPlayerError("WORKER_MESSAGE", "WASM worker received an uncloneable message.");
        this.#rejectPending(error);
        if (this.initialized)
            this.options.onError(error);
    };
    #rejectPending(error) {
        for (const pending of this.pending.values()) {
            clearTimeout(pending.timer);
            pending.reject(error);
        }
        this.pending.clear();
    }
    #toError(error) {
        return new HevcPlayerError(error.code, error.message);
    }
}
//# sourceMappingURL=wasm-worker.js.map