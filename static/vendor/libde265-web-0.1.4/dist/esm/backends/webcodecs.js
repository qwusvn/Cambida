import { UnifiedVideoFrame } from "../frame.js";
import { HevcPlayerError } from "../errors.js";
export function needsWebCodecsGeometryCorrection(frame, expectedDisplayWidth, expectedDisplayHeight) {
    if (!Number.isInteger(expectedDisplayWidth)
        || !Number.isInteger(expectedDisplayHeight)
        || expectedDisplayWidth <= 0
        || expectedDisplayHeight <= 0) {
        return false;
    }
    // The caller-provided display area must fit inside the decoded coded frame.
    // This supports normal codec alignment padding while rejecting dimensions
    // that cannot describe the decoded resource.
    if (expectedDisplayWidth > frame.codedWidth
        || expectedDisplayHeight > frame.codedHeight) {
        return false;
    }
    const rect = frame.visibleRect;
    if (rect === null)
        return true;
    return rect.x !== 0
        || rect.y !== 0
        || rect.width !== expectedDisplayWidth
        || rect.height !== expectedDisplayHeight
        || frame.displayWidth !== expectedDisplayWidth
        || frame.displayHeight !== expectedDisplayHeight;
}
function correctWebCodecsGeometry(nativeFrame, expectedDisplayWidth, expectedDisplayHeight) {
    if (!needsWebCodecsGeometryCorrection(nativeFrame, expectedDisplayWidth, expectedDisplayHeight)) {
        return nativeFrame;
    }
    try {
        const init = {
            visibleRect: {
                x: 0,
                y: 0,
                width: expectedDisplayWidth,
                height: expectedDisplayHeight
            },
            displayWidth: expectedDisplayWidth,
            displayHeight: expectedDisplayHeight
        };
        if (nativeFrame.duration !== null) {
            init.duration = nativeFrame.duration;
        }
        const correctedFrame = new globalThis.VideoFrame(nativeFrame, init);
        nativeFrame.close();
        return correctedFrame;
    }
    catch {
        // Preserve the decoded frame if a browser rejects the cloning overload.
        return nativeFrame;
    }
}
export class WebCodecsBackend {
    options;
    decoder = null;
    config = null;
    closed = false;
    constructor(options) {
        this.options = options;
    }
    get decodeQueueSize() {
        return this.decoder?.decodeQueueSize ?? 0;
    }
    static async isSupported(config) {
        if (typeof globalThis.VideoDecoder !== "function")
            return false;
        try {
            const result = await globalThis.VideoDecoder.isConfigSupported(config);
            return result.supported === true;
        }
        catch {
            return false;
        }
    }
    async init() {
        const config = {
            codec: this.options.codec,
            optimizeForLatency: true,
            hardwareAcceleration: this.options.hardwareAcceleration ?? "no-preference"
        };
        if (this.options.codedWidth !== undefined) {
            config.codedWidth = this.options.codedWidth;
        }
        if (this.options.codedHeight !== undefined) {
            config.codedHeight = this.options.codedHeight;
        }
        if (!(await WebCodecsBackend.isSupported(config))) {
            throw new HevcPlayerError("WEBCODECS_UNSUPPORTED", `WebCodecs does not support HEVC configuration "${config.codec}".`);
        }
        this.decoder = new globalThis.VideoDecoder({
            output: (nativeFrame) => {
                const outputFrame = correctWebCodecsGeometry(nativeFrame, this.options.expectedDisplayWidth, this.options.expectedDisplayHeight);
                const frameInit = {
                    timestamp: outputFrame.timestamp,
                    codedWidth: outputFrame.codedWidth,
                    codedHeight: outputFrame.codedHeight,
                    displayWidth: outputFrame.displayWidth,
                    displayHeight: outputFrame.displayHeight,
                    backend: "webcodecs",
                    nativeFrame: outputFrame
                };
                const frame = new UnifiedVideoFrame(outputFrame.duration === null
                    ? frameInit
                    : { ...frameInit, duration: outputFrame.duration });
                this.options.onFrame(frame);
            },
            error: (error) => this.options.onError(new HevcPlayerError("WEBCODECS_DECODE", error.message, error))
        });
        this.decoder.addEventListener("dequeue", this.options.onDequeue);
        this.config = config;
        this.decoder.configure(config);
    }
    decode(accessUnit, timestamp, duration) {
        if (!this.decoder || this.closed) {
            throw new HevcPlayerError("DECODER_CLOSED", "WebCodecs decoder is not open.");
        }
        const init = {
            type: accessUnit.key ? "key" : "delta",
            timestamp,
            data: accessUnit.data
        };
        if (duration !== undefined)
            init.duration = duration;
        this.decoder.decode(new globalThis.EncodedVideoChunk(init));
    }
    async flush() {
        await this.decoder?.flush();
    }
    reset() {
        if (this.decoder?.state === "configured" && this.config) {
            this.decoder.reset();
            this.decoder.configure(this.config);
        }
    }
    close() {
        this.closed = true;
        if (this.decoder && this.decoder.state !== "closed") {
            this.decoder.removeEventListener("dequeue", this.options.onDequeue);
            this.decoder.close();
        }
    }
}
//# sourceMappingURL=webcodecs.js.map