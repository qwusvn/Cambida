declare function nalType(nal: Uint8Array): number;
/**
 * Incrementally converts an arbitrary Annex-B byte stream into HEVC access units.
 * The parser keeps one trailing NAL/access unit buffered until a following
 * boundary is known. Call flush() when the stream ends.
 */
export declare class AnnexBAccessUnitParser {
    #private;
    push(data: BinaryData): HevcAccessUnit[];
    flush(): HevcAccessUnit[];
    reset(): void;
}
export declare const hevcNalType: typeof nalType;
export declare function isHevcKeyAccessUnit(data: BinaryData): boolean;
import type { BinaryData, HevcAccessUnit } from "./types.js";
export {};
//# sourceMappingURL=annexb.d.ts.map