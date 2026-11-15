export class EventHub {
    #listeners = new Map();
    on(type, listener) {
        const listeners = this.#listeners.get(type) ?? new Set();
        listeners.add(listener);
        this.#listeners.set(type, listeners);
        return () => this.off(type, listener);
    }
    off(type, listener) {
        this.#listeners.get(type)?.delete(listener);
    }
    emit(type, detail) {
        for (const listener of this.#listeners.get(type) ?? []) {
            listener(detail);
        }
    }
    clear() {
        this.#listeners.clear();
    }
}
//# sourceMappingURL=events.js.map