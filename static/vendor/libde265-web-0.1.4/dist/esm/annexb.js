const VCL_MAX = 31;
const IRAP_MIN = 16;
const IRAP_MAX = 23;
const VPS = 32;
const SPS = 33;
const PPS = 34;
const AUD = 35;
const EOS = 36;
const EOB = 37;
const PREFIX_SEI = 39;
function concat(parts) {
    const size = parts.reduce((sum, part) => sum + part.byteLength, 0);
    const output = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) {
        output.set(part, offset);
        offset += part.byteLength;
    }
    return output;
}
function toUint8Array(data) {
    return data instanceof ArrayBuffer
        ? new Uint8Array(data)
        : new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
}
function startCodeAt(data, index) {
    if (data[index] !== 0 || data[index + 1] !== 0)
        return 0;
    if (data[index + 2] === 1)
        return 3;
    return data[index + 2] === 0 && data[index + 3] === 1 ? 4 : 0;
}
function findStartCode(data, from) {
    for (let i = from; i + 2 < data.length; i += 1) {
        if (startCodeAt(data, i))
            return i;
    }
    return -1;
}
function nalType(nal) {
    const offset = startCodeAt(nal, 0);
    return offset && nal.length >= offset + 2 ? (nal[offset] >> 1) & 0x3f : -1;
}
function firstSlice(nal) {
    const offset = startCodeAt(nal, 0);
    return offset > 0 && nal.length > offset + 2 && (nal[offset + 2] & 0x80) !== 0;
}
function isPrefix(type) {
    return type === VPS || type === SPS || type === PPS || type === AUD || type === PREFIX_SEI;
}
function hasType(nals, expected) {
    return nals.some((nal) => nalType(nal) === expected);
}
/**
 * Incrementally converts an arbitrary Annex-B byte stream into HEVC access units.
 * The parser keeps one trailing NAL/access unit buffered until a following
 * boundary is known. Call flush() when the stream ends.
 */
export class AnnexBAccessUnitParser {
    #storage = new Uint8Array(4096);
    #length = 0;
    #current = [];
    #nextPrefix = [];
    #hasVcl = false;
    #parameterSets = new Map();
    push(data) {
        const input = toUint8Array(data);
        this.#append(input);
        return this.#drainNals(false);
    }
    flush() {
        const output = this.#drainNals(true);
        const final = this.#emitCurrent();
        if (final)
            output.push(final);
        this.#length = 0;
        this.#nextPrefix = [];
        return output;
    }
    reset() {
        this.#length = 0;
        if (this.#storage.byteLength > 1024 * 1024) {
            this.#storage = new Uint8Array(4096);
        }
        this.#current = [];
        this.#nextPrefix = [];
        this.#hasVcl = false;
        this.#parameterSets.clear();
    }
    #drainNals(flush) {
        const output = [];
        let bytes = this.#storage.subarray(0, this.#length);
        let first = findStartCode(bytes, 0);
        if (first < 0) {
            if (this.#length > 3)
                this.#keepRemainder(this.#length - 3);
            return output;
        }
        if (first > 0) {
            this.#keepRemainder(first);
            bytes = this.#storage.subarray(0, this.#length);
        }
        let cursor = 0;
        while (true) {
            const codeLength = startCodeAt(bytes, cursor);
            const next = findStartCode(bytes, cursor + codeLength);
            if (next < 0) {
                if (flush && bytes.length > cursor + codeLength) {
                    const emitted = this.#acceptNal(bytes.slice(cursor));
                    if (emitted)
                        output.push(emitted);
                    this.#length = 0;
                }
                else {
                    this.#keepRemainder(cursor);
                }
                break;
            }
            const emitted = this.#acceptNal(bytes.slice(cursor, next));
            if (emitted)
                output.push(emitted);
            cursor = next;
        }
        return output;
    }
    #append(input) {
        const required = this.#length + input.byteLength;
        if (required > this.#storage.byteLength) {
            let capacity = this.#storage.byteLength;
            while (capacity < required)
                capacity *= 2;
            const expanded = new Uint8Array(capacity);
            expanded.set(this.#storage.subarray(0, this.#length));
            this.#storage = expanded;
        }
        this.#storage.set(input, this.#length);
        this.#length = required;
    }
    #keepRemainder(from) {
        if (from <= 0)
            return;
        this.#storage.copyWithin(0, from, this.#length);
        this.#length -= from;
    }
    #acceptNal(nal) {
        const type = nalType(nal);
        if (type < 0)
            return null;
        if (type === VPS || type === SPS || type === PPS) {
            this.#parameterSets.set(type, nal.slice());
        }
        if (type === AUD && (this.#hasVcl || this.#current.length)) {
            const accessUnit = this.#emitCurrent();
            this.#current.push(...this.#nextPrefix, nal);
            this.#nextPrefix = [];
            return accessUnit;
        }
        if (type <= VCL_MAX && firstSlice(nal) && this.#hasVcl) {
            const accessUnit = this.#emitCurrent();
            this.#current.push(...this.#nextPrefix, nal);
            this.#nextPrefix = [];
            this.#hasVcl = true;
            return accessUnit;
        }
        if (this.#hasVcl && isPrefix(type)) {
            this.#nextPrefix.push(nal);
        }
        else {
            this.#current.push(nal);
            if (type <= VCL_MAX)
                this.#hasVcl = true;
        }
        if ((type === EOS || type === EOB) && this.#hasVcl) {
            return this.#emitCurrent();
        }
        return null;
    }
    #emitCurrent() {
        if (!this.#current.length || !this.#hasVcl)
            return null;
        const key = this.#current.some((nal) => {
            const type = nalType(nal);
            return type >= IRAP_MIN && type <= IRAP_MAX;
        });
        let nals = this.#current;
        if (key) {
            const missing = [VPS, SPS, PPS]
                .filter((type) => !hasType(nals, type))
                .map((type) => this.#parameterSets.get(type))
                .filter((nal) => nal !== undefined);
            if (missing.length)
                nals = [...missing, ...nals];
        }
        const accessUnit = { data: concat(nals), key };
        this.#current = [];
        this.#hasVcl = false;
        return accessUnit;
    }
}
export const hevcNalType = nalType;
export function isHevcKeyAccessUnit(data) {
    const bytes = toUint8Array(data);
    let offset = findStartCode(bytes, 0);
    while (offset >= 0) {
        const type = nalType(bytes.subarray(offset));
        if (type >= IRAP_MIN && type <= IRAP_MAX)
            return true;
        const codeLength = startCodeAt(bytes, offset);
        offset = findStartCode(bytes, offset + codeLength);
    }
    return false;
}
//# sourceMappingURL=annexb.js.map