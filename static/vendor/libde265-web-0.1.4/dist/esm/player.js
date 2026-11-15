import { AnnexBAccessUnitParser, isHevcKeyAccessUnit } from "./annexb.js";
import { CanvasRenderer, createRenderer } from "./renderer.js";
import { EventHub } from "./events.js";
import { HevcPlayerError } from "./errors.js";
import { WebCodecsBackend } from "./backends/webcodecs.js";
import { WasmBackend } from "./backends/wasm.js";
import { WasmWorkerBackend } from "./backends/wasm-worker.js";
const MODES = new Set(["auto", "webcodecs", "wasm"]);
const WORKER_MODES = new Set(["auto", "disabled", "required"]);
const RENDERER_MODES = new Set(["auto", "canvas2d", "webgl"]);
const WORKER_FRAME_OUTPUTS = new Set(["planes", "metadata"]);
const DEFAULT_LOW_LATENCY = {
    enabled: true,
    maxInputFrames: 12,
    maxInputBytes: 8 * 1024 * 1024,
    maxDecodeQueue: 4,
    maxQueueAgeMs: 250,
    dropPolicy: "until-key"
};
function now() {
    return globalThis.performance?.now() ?? Date.now();
}
function normalizeLowLatency(value) {
    const supplied = typeof value === "object" ? value : {};
    const normalized = {
        ...DEFAULT_LOW_LATENCY,
        ...supplied,
        enabled: typeof value === "boolean"
            ? value
            : supplied.enabled ?? DEFAULT_LOW_LATENCY.enabled
    };
    for (const name of [
        "maxInputFrames",
        "maxInputBytes",
        "maxDecodeQueue",
        "maxQueueAgeMs"
    ]) {
        if (!Number.isFinite(normalized[name]) || normalized[name] <= 0) {
            throw new HevcPlayerError("INVALID_LOW_LATENCY", `${name} must be a positive finite number.`);
        }
    }
    normalized.maxInputFrames = Math.floor(normalized.maxInputFrames);
    normalized.maxInputBytes = Math.floor(normalized.maxInputBytes);
    normalized.maxDecodeQueue = Math.floor(normalized.maxDecodeQueue);
    return normalized;
}
export class HevcPlayer {
    options;
    events = new EventHub();
    parser = new AnnexBAccessUnitParser();
    renderer = null;
    rendererKind = null;
    wasmExecution = null;
    backend = null;
    state = "idle";
    decoder = null;
    socket = null;
    socketMessageChain = Promise.resolve();
    nextTimestamp = 0;
    inputQueue = [];
    inputQueueBytes = 0;
    drainScheduled = false;
    drainTimer = null;
    inputIdleWaiters = [];
    waitingForKeyframe = false;
    pressureLevel = "normal";
    pendingFrames = [];
    rendering = false;
    renderPromise = Promise.resolve();
    decodedFrames = 0;
    renderedFrames = 0;
    droppedInputFrames = 0;
    droppedRenderFrames = 0;
    constructor(options) {
        if (!options || !MODES.has(options.mode ?? "auto")) {
            throw new HevcPlayerError("INVALID_MODE", "mode must be auto, webcodecs, or wasm.");
        }
        if (!WORKER_MODES.has(options.wasmWorker ?? "auto")) {
            throw new HevcPlayerError("INVALID_WORKER_MODE", "wasmWorker must be auto, disabled, or required.");
        }
        if (!RENDERER_MODES.has(options.renderer ?? "auto")) {
            throw new HevcPlayerError("INVALID_RENDERER", "renderer must be auto, canvas2d, or webgl.");
        }
        if (!WORKER_FRAME_OUTPUTS.has(options.workerFrameOutput ?? "planes")) {
            throw new HevcPlayerError("INVALID_FRAME_OUTPUT", "workerFrameOutput must be planes or metadata.");
        }
        this.options = {
            mode: "auto",
            codec: "hev1.1.6.L93.B0",
            frameDuration: 33_333,
            render: true,
            wasmWorker: "auto",
            renderer: "auto",
            workerFrameOutput: "planes",
            ...options,
            lowLatency: normalizeLowLatency(options.lowLatency)
        };
    }
    on(type, listener) {
        return this.events.on(type, listener);
    }
    async init() {
        if (this.state !== "idle")
            return this;
        this.state = "initializing";
        const backendOptions = {
            ...this.options,
            onFrame: (frame) => this.#handleFrame(frame),
            onError: (error) => this.#handleError(error),
            onDequeue: () => this.#scheduleDrain()
        };
        try {
            if (this.options.mode !== "wasm") {
                try {
                    const webcodecs = new WebCodecsBackend(backendOptions);
                    await webcodecs.init();
                    this.decoder = webcodecs;
                    this.backend = "webcodecs";
                }
                catch (error) {
                    if (this.options.mode === "webcodecs")
                        throw error;
                    this.events.emit("fallback", { from: "webcodecs", to: "wasm", error });
                }
            }
            if (!this.decoder) {
                if (this.options.wasmWorker !== "disabled") {
                    const worker = new WasmWorkerBackend(backendOptions);
                    try {
                        await worker.init();
                        this.decoder = worker;
                        this.wasmExecution = "worker";
                    }
                    catch (error) {
                        if (worker.canvasTransferred || this.options.wasmWorker === "required") {
                            worker.close();
                            throw error;
                        }
                        worker.close();
                        this.events.emit("workerfallback", {
                            from: "worker",
                            to: "main",
                            error
                        });
                    }
                }
                if (!this.decoder) {
                    const wasm = new WasmBackend(backendOptions);
                    await wasm.init();
                    this.decoder = wasm;
                    this.wasmExecution = "main";
                }
                this.backend = "wasm";
            }
            if (this.options.canvas && this.options.render) {
                if (this.decoder.handlesRendering) {
                    this.rendererKind = this.decoder.rendererKind ?? null;
                }
                else {
                    this.renderer = this.backend === "webcodecs"
                        ? new CanvasRenderer(this.options.canvas)
                        : createRenderer(this.options.canvas, this.options.renderer);
                    this.rendererKind = this.renderer.kind;
                }
            }
            this.state = "ready";
            this.events.emit("ready", { backend: this.backend });
            return this;
        }
        catch (error) {
            this.state = "error";
            this.#handleError(error);
            throw error;
        }
    }
    async connect(url = this.options.url, protocols = this.options.protocols) {
        await this.init();
        if (!url)
            throw new HevcPlayerError("WEBSOCKET_URL", "A WebSocket URL is required.");
        this.disconnect();
        const socket = new WebSocket(url, protocols);
        socket.binaryType = "arraybuffer";
        this.socket = socket;
        socket.addEventListener("open", () => this.events.emit("open", { url }));
        socket.addEventListener("message", (event) => {
            this.#queueSocketMessage(event.data);
        });
        socket.addEventListener("close", (event) => {
            this.events.emit("close", { code: event.code, reason: event.reason });
        });
        socket.addEventListener("error", () => {
            this.#handleError(new HevcPlayerError("WEBSOCKET", `WebSocket error for ${url}.`));
        });
        return new Promise((resolve, reject) => {
            socket.addEventListener("open", () => resolve(this), { once: true });
            socket.addEventListener("error", () => reject(new HevcPlayerError("WEBSOCKET_CONNECT", `Could not connect to ${url}.`)), { once: true });
        });
    }
    attach(socket) {
        if (!(socket instanceof WebSocket)) {
            throw new HevcPlayerError("WEBSOCKET_INSTANCE", "attach() expects a WebSocket.");
        }
        this.socket = socket;
        socket.binaryType = "arraybuffer";
        socket.addEventListener("message", (event) => {
            this.#queueSocketMessage(event.data);
        });
        return this;
    }
    push(data, metadata = {}) {
        if (this.state !== "ready") {
            throw new HevcPlayerError("NOT_READY", "Call and await init() before push().");
        }
        const accessUnits = this.parser.push(data);
        let timestamp = metadata.timestamp;
        const duration = metadata.duration ?? this.options.frameDuration;
        for (const accessUnit of accessUnits) {
            this.#enqueue(accessUnit, timestamp === undefined ? metadata : { ...metadata, timestamp });
            if (timestamp !== undefined)
                timestamp += duration;
        }
        return accessUnits.length;
    }
    pushAccessUnit(data, metadata = {}) {
        if (this.state !== "ready") {
            throw new HevcPlayerError("NOT_READY", "Call and await init() before pushAccessUnit().");
        }
        const bytes = data instanceof ArrayBuffer
            ? new Uint8Array(data)
            : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
        if (!bytes.byteLength) {
            throw new HevcPlayerError("EMPTY_ACCESS_UNIT", "Access Unit cannot be empty.");
        }
        this.#enqueue({
            data: bytes,
            key: metadata.key ?? isHevcKeyAccessUnit(bytes)
        }, metadata);
    }
    async flush() {
        if (!this.decoder)
            return;
        await this.socketMessageChain;
        for (const accessUnit of this.parser.flush())
            this.#enqueue(accessUnit, {});
        await this.#waitForInputIdle();
        await this.decoder.flush();
        await this.renderPromise;
    }
    reset() {
        this.parser.reset();
        this.decoder?.reset();
        this.#clearInputQueue("waiting-key", false);
        this.waitingForKeyframe = false;
        this.nextTimestamp = 0;
        this.#updatePressure();
    }
    disconnect(code = 1000, reason = "client disconnect") {
        if (this.socket && this.socket.readyState < WebSocket.CLOSING) {
            this.socket.close(code, reason);
        }
        this.socket = null;
    }
    close() {
        if (this.state === "closed")
            return;
        this.disconnect();
        if (this.drainTimer !== null)
            clearTimeout(this.drainTimer);
        this.drainTimer = null;
        this.#clearInputQueue("waiting-key", false);
        for (const frame of this.pendingFrames.splice(0))
            frame.close();
        this.decoder?.close();
        this.decoder = null;
        this.renderer?.close();
        this.renderer = null;
        this.state = "closed";
        this.events.clear();
    }
    getStats() {
        return {
            backend: this.backend,
            renderer: this.rendererKind,
            wasmExecution: this.wasmExecution,
            inputQueueFrames: this.inputQueue.length,
            inputQueueBytes: this.inputQueueBytes,
            decodeQueueSize: this.decoder?.decodeQueueSize ?? 0,
            queueAgeMs: this.inputQueue.length
                ? Math.max(0, now() - this.inputQueue[0].queuedAt)
                : 0,
            waitingForKeyframe: this.waitingForKeyframe,
            decodedFrames: this.decodedFrames,
            renderedFrames: this.renderedFrames,
            droppedInputFrames: this.droppedInputFrames,
            droppedRenderFrames: this.droppedRenderFrames
        };
    }
    #enqueue(accessUnit, metadata) {
        if (!this.decoder) {
            throw new HevcPlayerError("NOT_READY", "Decoder is not initialized.");
        }
        const timestamp = metadata.timestamp ?? this.nextTimestamp;
        const duration = metadata.duration ?? this.options.frameDuration;
        this.nextTimestamp = timestamp + duration;
        const queued = {
            accessUnit,
            timestamp,
            duration,
            queuedAt: now(),
            ...(metadata.sequence === undefined ? {} : { sequence: metadata.sequence })
        };
        if (this.waitingForKeyframe) {
            if (!accessUnit.key) {
                this.#dropInput(queued, "waiting-key");
                return;
            }
            this.waitingForKeyframe = false;
            this.decoder.reset();
        }
        this.inputQueue.push(queued);
        this.inputQueueBytes += accessUnit.data.byteLength;
        const oldestAge = now() - this.inputQueue[0].queuedAt;
        const overflow = this.inputQueue.length > this.options.lowLatency.maxInputFrames
            || this.inputQueueBytes > this.options.lowLatency.maxInputBytes;
        const stale = oldestAge > this.options.lowLatency.maxQueueAgeMs;
        if (this.options.lowLatency.enabled
            && this.options.lowLatency.dropPolicy === "until-key"
            && (overflow || stale)) {
            this.#recoverToKeyframe(overflow ? "input-overflow" : "queue-age", queued);
            return;
        }
        this.#emitQueueChange();
        this.#scheduleDrain();
    }
    #queueSocketMessage(data) {
        this.socketMessageChain = this.socketMessageChain.then(async () => {
            if (data instanceof ArrayBuffer) {
                this.push(new Uint8Array(data));
                return;
            }
            if (data instanceof Blob) {
                this.push(new Uint8Array(await data.arrayBuffer()));
                return;
            }
            throw new HevcPlayerError("WEBSOCKET_MESSAGE", "Expected binary WebSocket messages containing an Annex-B HEVC stream.");
        }).catch((error) => this.#handleError(error));
    }
    #scheduleDrain() {
        if (this.drainScheduled || !this.inputQueue.length || this.state === "closed")
            return;
        this.drainScheduled = true;
        queueMicrotask(() => {
            this.drainScheduled = false;
            this.#drainInput();
        });
    }
    #drainInput() {
        const decoder = this.decoder;
        if (!decoder || this.state === "closed")
            return;
        const started = now();
        while (this.inputQueue.length) {
            if (decoder.decodeQueueSize >= this.options.lowLatency.maxDecodeQueue) {
                this.#updatePressure();
                return;
            }
            const queued = this.inputQueue.shift();
            this.inputQueueBytes -= queued.accessUnit.data.byteLength;
            try {
                decoder.decode(queued.accessUnit, queued.timestamp, queued.duration);
            }
            catch (error) {
                this.#handleError(error);
                if (this.options.lowLatency.dropPolicy === "until-key") {
                    this.#recoverToKeyframe("input-overflow");
                    return;
                }
            }
            if (this.backend === "wasm" && now() - started >= 4 && this.inputQueue.length) {
                this.drainTimer = setTimeout(() => {
                    this.drainTimer = null;
                    this.#scheduleDrain();
                }, 0);
                this.#emitQueueChange();
                return;
            }
        }
        this.#emitQueueChange();
        this.#resolveInputIdle();
    }
    #recoverToKeyframe(reason, newest) {
        const keepNewestKey = newest?.accessUnit.key === true;
        this.decoder?.reset();
        if (keepNewestKey && newest && this.inputQueue.at(-1) === newest) {
            this.inputQueue.pop();
            this.inputQueueBytes -= newest.accessUnit.data.byteLength;
        }
        this.#clearInputQueue(reason, true);
        if (keepNewestKey && newest) {
            this.waitingForKeyframe = false;
            this.inputQueue.push(newest);
            this.inputQueueBytes = newest.accessUnit.data.byteLength;
            this.#emitQueueChange();
            this.#scheduleDrain();
            return;
        }
        this.waitingForKeyframe = true;
        this.events.emit("keyframerequest", { reason });
        this.#emitQueueChange();
    }
    #clearInputQueue(reason, emitDrops) {
        const queued = this.inputQueue;
        this.inputQueue = [];
        this.inputQueueBytes = 0;
        if (emitDrops) {
            for (const item of queued)
                this.#dropInput(item, reason);
        }
        this.#resolveInputIdle();
    }
    #dropInput(item, reason) {
        this.droppedInputFrames += 1;
        this.events.emit("drop", {
            stage: "input",
            reason,
            timestamp: item.timestamp,
            key: item.accessUnit.key,
            ...(item.sequence === undefined ? {} : { sequence: item.sequence })
        });
    }
    #waitForInputIdle() {
        if (!this.inputQueue.length)
            return Promise.resolve();
        return new Promise((resolve) => this.inputIdleWaiters.push(resolve));
    }
    #resolveInputIdle() {
        if (this.inputQueue.length)
            return;
        for (const resolve of this.inputIdleWaiters.splice(0))
            resolve();
    }
    #emitQueueChange() {
        const stats = this.getStats();
        this.events.emit("queuechange", stats);
        this.#updatePressure(stats);
    }
    #updatePressure(stats = this.getStats()) {
        const high = stats.waitingForKeyframe
            || stats.inputQueueFrames >= Math.ceil(this.options.lowLatency.maxInputFrames * 0.75)
            || stats.inputQueueBytes >= this.options.lowLatency.maxInputBytes * 0.75
            || stats.decodeQueueSize >= this.options.lowLatency.maxDecodeQueue;
        const level = high ? "high" : "normal";
        if (level === this.pressureLevel)
            return;
        this.pressureLevel = level;
        this.events.emit("backpressure", { level, stats });
    }
    #handleFrame(frame) {
        this.decodedFrames += 1;
        if (this.options.lowLatency.enabled
            && this.rendering
            && this.pendingFrames.length) {
            const dropped = this.pendingFrames.pop();
            this.droppedRenderFrames += 1;
            this.events.emit("drop", {
                stage: "render",
                reason: "render-backpressure",
                timestamp: dropped.timestamp
            });
            dropped.close();
        }
        this.pendingFrames.push(frame);
        if (!this.rendering) {
            this.renderPromise = this.#drainRender();
        }
    }
    async #drainRender() {
        this.rendering = true;
        try {
            while (this.pendingFrames.length) {
                const frame = this.pendingFrames.shift();
                try {
                    await this.options.onFrame?.(frame);
                    this.renderer?.render(frame);
                    this.events.emit("frame", frame);
                    this.renderedFrames += 1;
                }
                catch (error) {
                    this.#handleError(error);
                }
                finally {
                    if (this.options.autoCloseFrames !== false)
                        frame.close();
                }
            }
        }
        finally {
            this.rendering = false;
        }
    }
    #handleError(error) {
        const normalized = error instanceof HevcPlayerError
            ? error
            : new HevcPlayerError("UNKNOWN", error instanceof Error ? error.message : String(error), error);
        this.events.emit("error", normalized);
        this.options.onError?.(normalized);
    }
}
//# sourceMappingURL=player.js.map