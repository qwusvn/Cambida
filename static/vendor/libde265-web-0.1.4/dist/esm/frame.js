export class UnifiedVideoFrame {
    timestamp;
    duration;
    codedWidth;
    codedHeight;
    displayWidth;
    displayHeight;
    backend;
    _nativeFrame;
    _planes;
    _closed = false;
    constructor(init) {
        this.timestamp = init.timestamp;
        this.duration = init.duration;
        this.codedWidth = init.codedWidth;
        this.codedHeight = init.codedHeight;
        this.displayWidth = init.displayWidth ?? init.codedWidth;
        this.displayHeight = init.displayHeight ?? init.codedHeight;
        this.backend = init.backend;
        this._nativeFrame = init.nativeFrame;
        this._planes = init.planes;
    }
    get closed() {
        return this._closed;
    }
    /** Native WebCodecs frame, available only while a WebCodecs frame is open. */
    get nativeFrame() {
        return this._nativeFrame;
    }
    /** Decoded I420 planes, available only while a WASM frame is open. */
    get planes() {
        return this._planes;
    }
    close() {
        if (this._closed)
            return;
        this._closed = true;
        this._nativeFrame?.close();
        this._nativeFrame = undefined;
        this._planes = undefined;
    }
}
//# sourceMappingURL=frame.js.map