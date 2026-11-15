# 许可

[English](../licensing.md) | 简体中文

本仓库自行编写的 JavaScript、TypeScript 声明、构建脚本和 `wasm/glue.cpp`
采用 MIT License。

`third_party/libde265` 是独立 Git submodule，当前固定为 v1.1.1。libde265 库采用
GNU Lesser General Public License 3.0 或更高版本，具体以子模块内 `COPYING`
及源文件声明为准。分发链接了 libde265 的 WASM 产物时，应履行 LGPL 对应义务，
包括保留许可和版权声明，并为用户修改/重新链接库提供合适机制。

npm 包同时包含 libde265 对应源码、`wasm/glue.cpp`、配置头文件和构建脚本。完整的
第三方版权、固定版本及源码位置见仓库根目录的 `NOTICE.md`。根目录 `LICENSE` 只覆盖
本项目自行编写的 MIT 代码，不覆盖 libde265。

HEVC/H.265 还可能涉及专利许可和版税，开源软件许可不等于获得专利许可。面向特定
国家/地区、设备、内容或商业模式发布前，应由合格法律顾问评估。本文不是法律意见。
