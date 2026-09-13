package main

// web.go — 终端页静态资源嵌入 (go:embed)。
// xterm.js / xterm.css / fit addon 在构建时下载放入 web/ (Dockerfile),
// 与 ssh_terminal.html 一起打进二进制, 单文件交付。

import (
	"embed"
	"io/fs"
)

//go:embed web/*
var webFiles embed.FS

// webFS — 根映射: /console/ssh_terminal.html 等。
var webFS fs.FS = func() fs.FS {
	sub, err := fs.Sub(webFiles, "web")
	if err != nil {
		panic(err)
	}
	return sub
}()
