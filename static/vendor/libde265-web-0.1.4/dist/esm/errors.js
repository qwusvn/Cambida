export class HevcPlayerError extends Error {
    code;
    constructor(code, message, cause) {
        super(message, cause === undefined ? undefined : { cause });
        this.name = "HevcPlayerError";
        this.code = code;
    }
}
//# sourceMappingURL=errors.js.map