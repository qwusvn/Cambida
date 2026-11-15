#include <emscripten/emscripten.h>
#include <libde265/de265.h>

#include <cstdint>

struct HevcDecoder {
  de265_decoder_context* decoder = nullptr;
  const de265_image* frame = nullptr;
  int more = 0;
};

static int start_code_length(const uint8_t* data, int length, int offset) {
  if (offset + 2 >= length || data[offset] != 0 || data[offset + 1] != 0) {
    return 0;
  }
  if (data[offset + 2] == 1) return 3;
  return offset + 3 < length && data[offset + 2] == 0 && data[offset + 3] == 1
      ? 4
      : 0;
}

static int find_start_code(const uint8_t* data, int length, int from) {
  for (int offset = from; offset + 2 < length; ++offset) {
    if (start_code_length(data, length, offset) != 0) return offset;
  }
  return -1;
}

extern "C" {

EMSCRIPTEN_KEEPALIVE
HevcDecoder* hevc_decoder_create(int threads) {
  auto* wrapper = new HevcDecoder();
  wrapper->decoder = de265_new_decoder();
  if (!wrapper->decoder) {
    delete wrapper;
    return nullptr;
  }
  if (threads > 0) {
    const de265_error error = de265_start_worker_threads(wrapper->decoder, threads);
    if (error != DE265_OK &&
        error != DE265_WARNING_NUMBER_OF_THREADS_LIMITED_TO_MAXIMUM) {
      de265_free_decoder(wrapper->decoder);
      delete wrapper;
      return nullptr;
    }
  }
  return wrapper;
}

EMSCRIPTEN_KEEPALIVE
void hevc_decoder_destroy(HevcDecoder* wrapper) {
  if (!wrapper) return;
  if (wrapper->decoder) de265_free_decoder(wrapper->decoder);
  delete wrapper;
}

EMSCRIPTEN_KEEPALIVE
int hevc_decoder_push(
    HevcDecoder* wrapper,
    const uint8_t* data,
    int length,
    double timestamp) {
  if (!wrapper || !wrapper->decoder || !data || length <= 0) {
    return DE265_ERROR_PARAMETER_PARSING;
  }
  const de265_PTS pts = static_cast<de265_PTS>(timestamp);
  int cursor = find_start_code(data, length, 0);
  if (cursor < 0) return DE265_ERROR_PARAMETER_PARSING;
  bool pushed = false;
  while (cursor >= 0) {
    const int code_length = start_code_length(data, length, cursor);
    const int nal_start = cursor + code_length;
    const int next = find_start_code(data, length, nal_start);
    const int nal_end = next >= 0 ? next : length;
    if (nal_end > nal_start) {
      const de265_error error = de265_push_NAL(
          wrapper->decoder,
          data + nal_start,
          nal_end - nal_start,
          pts,
          nullptr);
      if (error != DE265_OK) return static_cast<int>(error);
      pushed = true;
    }
    cursor = next;
  }
  if (!pushed) return DE265_ERROR_PARAMETER_PARSING;
  de265_push_end_of_frame(wrapper->decoder);
  wrapper->more = 1;
  return DE265_OK;
}

EMSCRIPTEN_KEEPALIVE
int hevc_decoder_flush(HevcDecoder* wrapper) {
  if (!wrapper || !wrapper->decoder) return DE265_ERROR_LIBRARY_NOT_INITIALIZED;
  const de265_error error = de265_flush_data(wrapper->decoder);
  wrapper->more = error == DE265_OK ? 1 : 0;
  return static_cast<int>(error);
}

EMSCRIPTEN_KEEPALIVE
int hevc_decoder_decode(HevcDecoder* wrapper) {
  if (!wrapper || !wrapper->decoder) return DE265_ERROR_LIBRARY_NOT_INITIALIZED;
  wrapper->more = 0;
  return static_cast<int>(de265_decode(wrapper->decoder, &wrapper->more));
}

EMSCRIPTEN_KEEPALIVE
int hevc_decoder_has_more(const HevcDecoder* wrapper) {
  return wrapper ? wrapper->more : 0;
}

EMSCRIPTEN_KEEPALIVE
void hevc_decoder_reset(HevcDecoder* wrapper) {
  if (!wrapper || !wrapper->decoder) return;
  wrapper->frame = nullptr;
  wrapper->more = 0;
  de265_reset(wrapper->decoder);
}

EMSCRIPTEN_KEEPALIVE
int hevc_decoder_next_frame(HevcDecoder* wrapper) {
  if (!wrapper || !wrapper->decoder) return 0;
  if (!wrapper->frame) {
    wrapper->frame = de265_peek_next_picture(wrapper->decoder);
  }
  return wrapper->frame ? 1 : 0;
}

EMSCRIPTEN_KEEPALIVE
void hevc_decoder_release_frame(HevcDecoder* wrapper) {
  if (!wrapper || !wrapper->decoder || !wrapper->frame) return;
  de265_release_next_picture(wrapper->decoder);
  wrapper->frame = nullptr;
}

EMSCRIPTEN_KEEPALIVE
int hevc_frame_width(const HevcDecoder* wrapper) {
  return wrapper && wrapper->frame ? de265_get_image_width(wrapper->frame, 0) : 0;
}

EMSCRIPTEN_KEEPALIVE
int hevc_frame_height(const HevcDecoder* wrapper) {
  return wrapper && wrapper->frame ? de265_get_image_height(wrapper->frame, 0) : 0;
}

EMSCRIPTEN_KEEPALIVE
int hevc_frame_plane_width(const HevcDecoder* wrapper, int plane) {
  return wrapper && wrapper->frame ? de265_get_image_width(wrapper->frame, plane) : 0;
}

EMSCRIPTEN_KEEPALIVE
int hevc_frame_plane_height(const HevcDecoder* wrapper, int plane) {
  return wrapper && wrapper->frame ? de265_get_image_height(wrapper->frame, plane) : 0;
}

EMSCRIPTEN_KEEPALIVE
int hevc_frame_stride(const HevcDecoder* wrapper, int plane) {
  if (!wrapper || !wrapper->frame) return 0;
  int stride = 0;
  de265_get_image_plane(wrapper->frame, plane, &stride);
  return stride;
}

EMSCRIPTEN_KEEPALIVE
const uint8_t* hevc_frame_plane(const HevcDecoder* wrapper, int plane) {
  return wrapper && wrapper->frame
      ? de265_get_image_plane(wrapper->frame, plane, nullptr)
      : nullptr;
}

EMSCRIPTEN_KEEPALIVE
int hevc_frame_bit_depth(const HevcDecoder* wrapper, int plane) {
  return wrapper && wrapper->frame
      ? de265_get_bits_per_pixel(wrapper->frame, plane)
      : 0;
}

EMSCRIPTEN_KEEPALIVE
int hevc_frame_full_range(const HevcDecoder* wrapper) {
  return wrapper && wrapper->frame
      ? de265_get_image_full_range_flag(wrapper->frame)
      : 0;
}

EMSCRIPTEN_KEEPALIVE
int hevc_frame_matrix_coefficients(const HevcDecoder* wrapper) {
  return wrapper && wrapper->frame
      ? de265_get_image_matrix_coefficients(wrapper->frame)
      : 2;
}

EMSCRIPTEN_KEEPALIVE
double hevc_frame_timestamp(const HevcDecoder* wrapper) {
  return wrapper && wrapper->frame
      ? static_cast<double>(de265_get_image_PTS(wrapper->frame))
      : 0.0;
}

}  // extern "C"
