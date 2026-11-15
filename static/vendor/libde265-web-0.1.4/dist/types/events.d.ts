type EventMap = object;
type Listener<T> = (detail: T) => void;
export declare class EventHub<Events extends EventMap> {
    #private;
    on<K extends keyof Events>(type: K, listener: Listener<Events[K]>): () => void;
    off<K extends keyof Events>(type: K, listener: Listener<Events[K]>): void;
    emit<K extends keyof Events>(type: K, detail: Events[K]): void;
    clear(): void;
}
export {};
//# sourceMappingURL=events.d.ts.map