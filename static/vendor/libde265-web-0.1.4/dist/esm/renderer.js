import { HevcPlayerError } from "./errors.js";
function yuvConversion(matrixCoefficients, height) {
    // H.273: 1 = BT.709, 5/6 = BT.470BG/SMPTE 170M, 9/10 = BT.2020.
    // For unspecified metadata (2), follow the common SD/HD decoder convention.
    if (matrixCoefficients === 1 || (matrixCoefficients === 2 && height > 576)) {
        return { rV: 1.5748, gU: -0.187324, gV: -0.468124, bU: 1.8556 };
    }
    if (matrixCoefficients === 9 || matrixCoefficients === 10) {
        return { rV: 1.4746, gU: -0.164553, gV: -0.571353, bU: 1.8814 };
    }
    return { rV: 1.402, gU: -0.344136, gV: -0.714136, bU: 1.772 };
}
function normalizeSample(sample, bitDepth, chroma, fullRange) {
    const maximum = (1 << bitDepth) - 1;
    const normalized = sample / maximum;
    if (fullRange) {
        const chromaOffset = chroma ? (1 << (bitDepth - 1)) / maximum : 0;
        return normalized - chromaOffset;
    }
    const shift = 1 << Math.max(0, bitDepth - 8);
    const offset = (chroma ? 128 : 16) * shift / maximum;
    const span = (chroma ? 224 : 219) * shift / maximum;
    return (normalized - offset) / span;
}
export class CanvasRenderer {
    kind = "canvas2d";
    canvas;
    context;
    imageData = null;
    constructor(canvas) {
        if (!canvas?.getContext) {
            throw new HevcPlayerError("INVALID_CANVAS", "A canvas element is required.");
        }
        this.canvas = canvas;
        const context = canvas.getContext("2d", { alpha: false, desynchronized: true });
        if (!context) {
            throw new HevcPlayerError("CANVAS_CONTEXT", "Could not create a 2D canvas context.");
        }
        this.context = context;
    }
    render(frame) {
        if (frame.closed)
            return;
        const width = frame.displayWidth;
        const height = frame.displayHeight;
        if (this.canvas.width !== width || this.canvas.height !== height) {
            this.canvas.width = width;
            this.canvas.height = height;
            this.imageData = null;
        }
        if (frame._nativeFrame) {
            this.context.drawImage(frame._nativeFrame, 0, 0, width, height);
            return;
        }
        this.#renderI420(frame, width, height);
    }
    close() {
        this.imageData = null;
    }
    #renderI420(frame, width, height) {
        const planes = frame._planes;
        if (!planes) {
            throw new HevcPlayerError("FRAME_FORMAT", "WASM frame does not contain I420 planes.");
        }
        const { y, u, v, yStride, uStride, vStride, yWidth, uWidth, vWidth, yHeight, uHeight, vHeight, yBitDepth = 8, uBitDepth = 8, vBitDepth = 8 } = planes;
        if (yWidth <= 0 || yHeight <= 0 || uWidth <= 0 || uHeight <= 0
            || vWidth <= 0 || vHeight <= 0) {
            throw new HevcPlayerError("FRAME_FORMAT", "WASM frame has invalid plane dimensions.");
        }
        if (!this.imageData || this.imageData.width !== width || this.imageData.height !== height) {
            this.imageData = this.context.createImageData(width, height);
        }
        const rgba = this.imageData.data;
        const commonI420 = yWidth === width && yHeight === height
            && uWidth === Math.ceil(width / 2) && vWidth === Math.ceil(width / 2)
            && uHeight === Math.ceil(height / 2) && vHeight === Math.ceil(height / 2);
        const yIs16 = yBitDepth > 8;
        const uIs16 = uBitDepth > 8;
        const vIs16 = vBitDepth > 8;
        const y16 = yIs16
            ? new Uint16Array(y.buffer, y.byteOffset, y.byteLength / 2)
            : null;
        const u16 = uIs16
            ? new Uint16Array(u.buffer, u.byteOffset, u.byteLength / 2)
            : null;
        const v16 = vIs16
            ? new Uint16Array(v.buffer, v.byteOffset, v.byteLength / 2)
            : null;
        const fullRange = planes.fullRange ?? false;
        const conversion = yuvConversion(planes.matrixCoefficients ?? 2, height);
        let index = 0;
        for (let row = 0; row < height; row += 1) {
            const yRow = Math.min(yHeight - 1, commonI420 ? row : Math.floor(row * yHeight / height));
            const uRow = Math.min(uHeight - 1, commonI420 ? row >> 1 : Math.floor(row * uHeight / height));
            const vRow = Math.min(vHeight - 1, commonI420 ? row >> 1 : Math.floor(row * vHeight / height));
            const yBase = yIs16 ? yRow * (yStride >> 1) : yRow * yStride;
            const uBase = uIs16 ? uRow * (uStride >> 1) : uRow * uStride;
            const vBase = vIs16 ? vRow * (vStride >> 1) : vRow * vStride;
            for (let col = 0; col < width; col += 1) {
                const yCol = commonI420 ? col : Math.min(yWidth - 1, Math.floor(col * yWidth / width));
                const uCol = commonI420 ? col >> 1 : Math.min(uWidth - 1, Math.floor(col * uWidth / width));
                const vCol = commonI420 ? col >> 1 : Math.min(vWidth - 1, Math.floor(col * vWidth / width));
                const yy = normalizeSample(yIs16
                    ? y16[yBase + yCol] ?? 0
                    : y[yBase + yCol] ?? 0, yBitDepth, false, fullRange);
                const uu = normalizeSample(uIs16
                    ? u16[uBase + uCol] ?? 0
                    : u[uBase + uCol] ?? 0, uBitDepth, true, fullRange);
                const vv = normalizeSample(vIs16
                    ? v16[vBase + vCol] ?? 0
                    : v[vBase + vCol] ?? 0, vBitDepth, true, fullRange);
                rgba[index] = 255 * (yy + conversion.rV * vv);
                rgba[index + 1] = 255 * (yy + conversion.gU * uu + conversion.gV * vv);
                rgba[index + 2] = 255 * (yy + conversion.bU * uu);
                rgba[index + 3] = 255;
                index += 4;
            }
        }
        this.context.putImageData(this.imageData, 0, 0);
    }
}
const VERTEX_SHADER = `#version 300 es
in vec2 aPosition;
in vec2 aTexCoord;
out vec2 vTexCoord;
void main() {
  gl_Position = vec4(aPosition, 0.0, 1.0);
  vTexCoord = aTexCoord;
}`;
const FRAGMENT_SHADER_8 = `#version 300 es
precision highp float;
in vec2 vTexCoord;
uniform sampler2D uY;
uniform sampler2D uU;
uniform sampler2D uV;
uniform vec2 uYScale;
uniform vec2 uUScale;
uniform vec2 uVScale;
uniform vec3 uRangeOffset;
uniform vec3 uRangeScale;
uniform mat3 uColorMatrix;
out vec4 outColor;
void main() {
  vec3 yuv = vec3(
    texture(uY, vTexCoord * uYScale).r,
    texture(uU, vTexCoord * uUScale).r,
    texture(uV, vTexCoord * uVScale).r
  );
  outColor = vec4(uColorMatrix * ((yuv - uRangeOffset) * uRangeScale), 1.0);
}`;
const FRAGMENT_SHADER_16 = `#version 300 es
precision highp float;
precision highp usampler2D;
in vec2 vTexCoord;
uniform usampler2D uY;
uniform usampler2D uU;
uniform usampler2D uV;
uniform vec2 uYScale;
uniform vec2 uUScale;
uniform vec2 uVScale;
uniform vec3 uSampleMax;
uniform vec3 uRangeOffset;
uniform vec3 uRangeScale;
uniform mat3 uColorMatrix;
out vec4 outColor;
void main() {
  vec3 yuv = vec3(
    float(texture(uY, vTexCoord * uYScale).r),
    float(texture(uU, vTexCoord * uUScale).r),
    float(texture(uV, vTexCoord * uVScale).r)
  ) / uSampleMax;
  outColor = vec4(uColorMatrix * ((yuv - uRangeOffset) * uRangeScale), 1.0);
}`;
export class WebGLRenderer {
    kind = "webgl2";
    canvas;
    gl;
    programs;
    textures;
    vertexBuffer;
    closed = false;
    constructor(canvas) {
        this.canvas = canvas;
        const gl = canvas.getContext("webgl2", {
            alpha: false,
            antialias: false,
            depth: false,
            desynchronized: false,
            preserveDrawingBuffer: true
        });
        if (!gl) {
            throw new HevcPlayerError("WEBGL_UNSUPPORTED", "WebGL2 is not available.");
        }
        this.gl = gl;
        this.programs = [
            this.#createProgram(FRAGMENT_SHADER_8),
            this.#createProgram(FRAGMENT_SHADER_16)
        ];
        const vertexBuffer = gl.createBuffer();
        if (!vertexBuffer) {
            throw new HevcPlayerError("WEBGL_INIT", "Could not create the WebGL vertex buffer.");
        }
        this.vertexBuffer = vertexBuffer;
        gl.bindBuffer(gl.ARRAY_BUFFER, vertexBuffer);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
            -1, -1, 0, 1,
            1, -1, 1, 1,
            -1, 1, 0, 0,
            1, 1, 1, 0
        ]), gl.STATIC_DRAW);
        const textures = [gl.createTexture(), gl.createTexture(), gl.createTexture()];
        if (textures.some((texture) => texture === null)) {
            throw new HevcPlayerError("WEBGL_INIT", "Could not create I420 textures.");
        }
        this.textures = textures;
        for (const texture of this.textures) {
            gl.bindTexture(gl.TEXTURE_2D, texture);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
            gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        }
        gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    }
    render(frame) {
        if (this.closed || frame.closed)
            return;
        const planes = frame._planes;
        if (!planes) {
            throw new HevcPlayerError("FRAME_FORMAT", "WebGL rendering requires WASM I420 planes.");
        }
        const gl = this.gl;
        const width = frame.displayWidth;
        const height = frame.displayHeight;
        if (this.canvas.width !== width || this.canvas.height !== height) {
            this.canvas.width = width;
            this.canvas.height = height;
        }
        gl.viewport(0, 0, width, height);
        const is16 = planes.yBitDepth > 8 || planes.uBitDepth > 8 || planes.vBitDepth > 8;
        if (is16
            && (planes.yBitDepth <= 8 || planes.uBitDepth <= 8 || planes.vBitDepth <= 8)) {
            throw new HevcPlayerError("FRAME_FORMAT", "WebGL rendering requires matching sample widths across I420 planes.");
        }
        const state = this.programs[is16 ? 1 : 0];
        gl.useProgram(state.program);
        gl.bindBuffer(gl.ARRAY_BUFFER, this.vertexBuffer);
        gl.enableVertexAttribArray(state.position);
        gl.vertexAttribPointer(state.position, 2, gl.FLOAT, false, 16, 0);
        gl.enableVertexAttribArray(state.texCoord);
        gl.vertexAttribPointer(state.texCoord, 2, gl.FLOAT, false, 16, 8);
        const data = [planes.y, planes.u, planes.v];
        const strides = [planes.yStride, planes.uStride, planes.vStride];
        const widths = [planes.yWidth, planes.uWidth, planes.vWidth];
        const heights = [planes.yHeight, planes.uHeight, planes.vHeight];
        const bitDepths = [planes.yBitDepth, planes.uBitDepth, planes.vBitDepth];
        for (let index = 0; index < 3; index += 1) {
            const bytesPerSample = bitDepths[index] > 8 ? 2 : 1;
            const textureWidth = strides[index] / bytesPerSample;
            const samples = bytesPerSample === 2
                ? new Uint16Array(data[index].buffer, data[index].byteOffset, data[index].byteLength / 2)
                : data[index];
            gl.activeTexture(gl.TEXTURE0 + index);
            gl.bindTexture(gl.TEXTURE_2D, this.textures[index]);
            if (is16) {
                gl.texImage2D(gl.TEXTURE_2D, 0, gl.R16UI, textureWidth, heights[index], 0, gl.RED_INTEGER, gl.UNSIGNED_SHORT, samples);
            }
            else {
                gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, textureWidth, heights[index], 0, gl.RED, gl.UNSIGNED_BYTE, samples);
            }
            gl.uniform2f(state.scales[index], widths[index] / textureWidth, 1);
        }
        if (state.sampleMax) {
            gl.uniform3f(state.sampleMax, (1 << planes.yBitDepth) - 1, (1 << planes.uBitDepth) - 1, (1 << planes.vBitDepth) - 1);
        }
        const maxima = bitDepths.map((depth) => (1 << depth) - 1);
        const offsets = bitDepths.map((depth, index) => {
            if (planes.fullRange) {
                return index === 0 ? 0 : (1 << (depth - 1)) / ((1 << depth) - 1);
            }
            return (index === 0 ? 16 : 128) * (1 << Math.max(0, depth - 8))
                / maxima[index];
        });
        const rangeScales = bitDepths.map((depth, index) => {
            if (planes.fullRange)
                return 1;
            return maxima[index]
                / ((index === 0 ? 219 : 224) * (1 << Math.max(0, depth - 8)));
        });
        gl.uniform3f(state.rangeOffset, offsets[0], offsets[1], offsets[2]);
        gl.uniform3f(state.rangeScale, rangeScales[0], rangeScales[1], rangeScales[2]);
        const conversion = yuvConversion(planes.matrixCoefficients ?? 2, height);
        gl.uniformMatrix3fv(state.colorMatrix, false, new Float32Array([
            1, 1, 1,
            0, conversion.gU, conversion.bU,
            conversion.rV, conversion.gV, 0
        ]));
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
    close() {
        if (this.closed)
            return;
        this.closed = true;
        for (const texture of this.textures)
            this.gl.deleteTexture(texture);
        for (const state of this.programs)
            this.gl.deleteProgram(state.program);
        this.gl.deleteBuffer(this.vertexBuffer);
    }
    #createProgram(fragmentSource) {
        const gl = this.gl;
        const vertex = this.#compileShader(gl.VERTEX_SHADER, VERTEX_SHADER);
        const fragment = this.#compileShader(gl.FRAGMENT_SHADER, fragmentSource);
        const program = gl.createProgram();
        if (!program) {
            throw new HevcPlayerError("WEBGL_INIT", "Could not create a WebGL program.");
        }
        gl.attachShader(program, vertex);
        gl.attachShader(program, fragment);
        gl.linkProgram(program);
        gl.deleteShader(vertex);
        gl.deleteShader(fragment);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
            const detail = gl.getProgramInfoLog(program) ?? "unknown link error";
            gl.deleteProgram(program);
            throw new HevcPlayerError("WEBGL_SHADER", `Could not link I420 shader: ${detail}`);
        }
        const position = gl.getAttribLocation(program, "aPosition");
        const texCoord = gl.getAttribLocation(program, "aTexCoord");
        const scales = ["uYScale", "uUScale", "uVScale"].map((name) => {
            const location = gl.getUniformLocation(program, name);
            if (!location) {
                throw new HevcPlayerError("WEBGL_SHADER", `Missing shader uniform ${name}.`);
            }
            return location;
        });
        gl.useProgram(program);
        for (const [index, name] of ["uY", "uU", "uV"].entries()) {
            gl.uniform1i(gl.getUniformLocation(program, name), index);
        }
        return {
            program,
            position,
            texCoord,
            scales,
            sampleMax: gl.getUniformLocation(program, "uSampleMax"),
            rangeOffset: this.#requiredUniform(program, "uRangeOffset"),
            rangeScale: this.#requiredUniform(program, "uRangeScale"),
            colorMatrix: this.#requiredUniform(program, "uColorMatrix")
        };
    }
    #requiredUniform(program, name) {
        const location = this.gl.getUniformLocation(program, name);
        if (!location)
            throw new HevcPlayerError("WEBGL_SHADER", `Missing shader uniform ${name}.`);
        return location;
    }
    #compileShader(type, source) {
        const shader = this.gl.createShader(type);
        if (!shader) {
            throw new HevcPlayerError("WEBGL_INIT", "Could not create a WebGL shader.");
        }
        this.gl.shaderSource(shader, source);
        this.gl.compileShader(shader);
        if (!this.gl.getShaderParameter(shader, this.gl.COMPILE_STATUS)) {
            const detail = this.gl.getShaderInfoLog(shader) ?? "unknown compile error";
            this.gl.deleteShader(shader);
            throw new HevcPlayerError("WEBGL_SHADER", `Could not compile I420 shader: ${detail}`);
        }
        return shader;
    }
}
export function createRenderer(canvas, mode = "auto") {
    if (mode !== "canvas2d") {
        try {
            return new WebGLRenderer(canvas);
        }
        catch (error) {
            if (mode === "webgl")
                throw error;
            if (!(error instanceof HevcPlayerError) || error.code !== "WEBGL_UNSUPPORTED") {
                throw error;
            }
        }
    }
    return new CanvasRenderer(canvas);
}
//# sourceMappingURL=renderer.js.map