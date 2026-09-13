package main

// admin.go — 节点凭证管理 API (节点凭证页后端):
//   GET    /api/ssh/credentials         — 凭据列表 (密码不回传, 只报状态)
//   DELETE /api/ssh/credentials?id=N    — 删除凭据
//   GET    /api/ssh/rotate-config       — 轮换配置
//   POST   /api/ssh/rotate-config       — 设置轮换 (enabled + days)
//   POST   /api/ssh/rotate-now          — 立即轮换某/全部节点
//   POST   /api/ssh/upload              — SFTP 上传 (multipart: file + worker_id + remote_path)
//   GET    /api/ssh/download            — SFTP 下载 (?worker_id=&path=) 流式回传
//
// 轮换配置落状态文件 (与凭据同文件, {"_rotate": {...}}), rotateLoop 每分钟检查
// 「距上次轮换 >= days」动态触发 (而不是启动时固定 ticker — 改天数立即生效)。

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path"
	"sort"
	"strings"
	"time"

	"github.com/pkg/sftp"
	"golang.org/x/crypto/ssh"
)

// ---------- 轮换配置 (持久化在状态文件) ----------

type RotateConfig struct {
	Enabled     bool      `json:"enabled"`
	Days        int       `json:"days"`
	LastApplied time.Time `json:"last_applied,omitempty"`
}


const kRotateKey = "_rotate"

// 轮换配置存在状态文件独立的 "_rotate" key。

func readRotate() RotateConfig {
	data, err := os.ReadFile(loadCfg.StateFile)
	if err != nil {
		return RotateConfig{Days: 7}
	}
	var raw map[string]json.RawMessage
	if json.Unmarshal(data, &raw) != nil {
		return RotateConfig{Days: 7}
	}
	if r, ok := raw[kRotateKey]; ok {
		var rc RotateConfig
		if json.Unmarshal(r, &rc) == nil {
			return rc
		}
	}
	return RotateConfig{Days: 7}
}

func writeRotate(rc RotateConfig) {
	data, err := os.ReadFile(loadCfg.StateFile)
	raw := map[string]interface{}{}
	if err == nil {
		_ = json.Unmarshal(data, &raw)
	}
	raw[kRotateKey] = rc
	out, _ := json.Marshal(raw)
	_ = os.WriteFile(loadCfg.StateFile, out, 0600)
}

// ---------- HTTP handlers ----------

type credItem struct {
	WorkerID  uint   `json:"worker_id"`
	IP        string `json:"ip"`
	Username  string `json:"username"`
	Port      int    `json:"port"`
	RotatedAt string `json:"rotated_at,omitempty"`
}

func handleCredList(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}
	credState.mu.RLock()
	items := make([]credItem, 0, len(credState.creds))
	for id, c := range credState.creds {
		items = append(items, credItem{
			WorkerID: id, IP: c.IP, Username: c.user(), Port: c.port(),
			RotatedAt: c.RotatedAt,
		})
	}
	credState.mu.RUnlock()
	sort.Slice(items, func(i, j int) bool { return items[i].WorkerID < items[j].WorkerID })
	rc := readRotate()
	writeJSON(w, map[string]interface{}{
		"items": items,
		"meta": map[string]interface{}{
			"rotate_enabled": rc.Enabled,
			"rotate_days":    rc.Days,
		},
	})
}

func handleCredDelete(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		http.Error(w, "DELETE only", http.StatusMethodNotAllowed)
		return
	}
	id, ok := queryUint(r, "id")
	if !ok {
		http.Error(w, "id required", http.StatusBadRequest)
		return
	}
	credState.mu.Lock()
	delete(credState.creds, id)
	credState.mu.Unlock()
	saveState()
	log.Printf("[credentials] worker %d deleted", id)
	writeJSON(w, map[string]bool{"ok": true})
}

func handleRotateConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet {
		rc := readRotate()
		writeJSON(w, map[string]interface{}{
			"enabled": rc.Enabled, "days": rc.Days,
			"last_applied": rc.LastApplied,
		})
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "GET/POST only", http.StatusMethodNotAllowed)
		return
	}
	var req RotateConfig
	if err := decodeJSON(r, &req); err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	if req.Days < 1 {
		req.Days = 7
	}
	if req.Days > 365 {
		req.Days = 365
	}
	rc := readRotate()
	rc.Enabled = req.Enabled
	rc.Days = req.Days
	// 天数改小或刚开启时, 立即评估 (rotateChecker 每 30s 检查到期)
	writeRotate(rc)
	log.Printf("[rotate] config updated: enabled=%v days=%d", rc.Enabled, rc.Days)
	writeJSON(w, map[string]interface{}{"enabled": rc.Enabled, "days": rc.Days})
}

func handleRotateNow(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	id := uint(0)
	if v, ok := queryUint(r, "id"); ok {
		id = v
	}
	n, firstErr := rotateWorkerPasswords(id)
	rc := readRotate()
	rc.LastApplied = time.Now().UTC()
	writeRotate(rc)
	if firstErr != nil {
		writeJSON(w, map[string]interface{}{"ok": false, "rotated": n, "message": firstErr.Error()})
		return
	}
	writeJSON(w, map[string]interface{}{"ok": true, "rotated": n})
}

// rotateWorkerPasswords — 立即轮换 (id=0 全部); 返回 (成功数, 首个错误)。
func rotateWorkerPasswords(id uint) (int, error) {
	credState.mu.RLock()
	var targets []uint
	for wid := range credState.creds {
		if id == 0 || wid == id {
			targets = append(targets, wid)
		}
	}
	credState.mu.RUnlock()
	n := 0
	var firstErr error
	for _, wid := range targets {
		credState.mu.RLock()
		c := credState.creds[wid]
		credState.mu.RUnlock()
		if c.IP == "" || c.Pw == "" {
			continue
		}
		np, err := genPassword()
		if err != nil {
			continue
		}
		if err := rotateRemotePassword(&c, np); err != nil {
			log.Printf("[rotate] worker %d (%s) 轮换失败: %v", wid, c.IP, err)
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		c.Pw = np
		c.RotatedAt = time.Now().UTC().Format(time.RFC3339)
		credState.mu.Lock()
		credState.creds[wid] = c
		credState.mu.Unlock()
		n++
		log.Printf("[rotate] worker %d (%s) 密码已轮换", wid, c.IP)
	}
	saveState()
	return n, firstErr
}

// rotateChecker — 周期评估到期 (页面改天数即时生效, 不重启网关)。
func rotateChecker(stop <-chan struct{}) {
	tk := time.NewTicker(30 * time.Second)
	defer tk.Stop()
	for {
		select {
		case <-stop:
			return
		case <-tk.C:
			rc := readRotate()
			if !rc.Enabled {
				continue
			}
			due := rc.LastApplied.IsZero() ||
				time.Since(rc.LastApplied) >= time.Duration(rc.Days)*24*time.Hour
			if !due {
				continue
			}
			n, err := rotateWorkerPasswords(0)
			rc.LastApplied = time.Now().UTC()
			writeRotate(rc)
			if err != nil {
				log.Printf("[rotate] scheduled rotate: %d ok, first err: %v", n, err)
			} else {
				log.Printf("[rotate] scheduled rotate: %d node(s)", n)
			}
		}
	}
}

// ---------- SFTP 传输 ----------

// sftpClient — 用节点凭据建立 SFTP 会话。
func sftpClient(workerID uint) (*sftp.Client, *ssh.Client, error) {
	c := getCred(workerID)
	if c == nil {
		return nil, nil, fmt.Errorf("该节点尚未配置 SSH 凭据")
	}
	cli, err := sshDial(c)
	if err != nil {
		_, msg := classifyErr(err)
		return nil, nil, fmt.Errorf("%s (%s:%d)", msg, c.IP, c.port())
	}
	sc, err := sftp.NewClient(cli)
	if err != nil {
		cli.Close()
		return nil, nil, fmt.Errorf("SFTP 会话失败: %v", err)
	}
	return sc, cli, nil
}

// handleUpload — multipart: file + worker_id + remote_path (目录或完整路径)。
func handleUpload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	if err := r.ParseMultipartForm(64 << 20); err != nil {
		http.Error(w, "bad multipart: "+err.Error(), http.StatusBadRequest)
		return
	}
	wid, ok := formUint(r, "worker_id")
	if !ok {
		http.Error(w, "worker_id required", http.StatusBadRequest)
		return
	}
	remotePath := strings.TrimSpace(r.FormValue("remote_path"))
	file, hdr, err := r.FormFile("file")
	if err != nil {
		http.Error(w, "file required", http.StatusBadRequest)
		return
	}
	defer file.Close()

	sc, cli, err := sftpClient(wid)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	defer sc.Close()
	defer cli.Close()

	// remote_path 为目录时拼文件名; 确保以 / 开头
	if remotePath == "" {
		remotePath = "/tmp"
	}
	if !strings.HasPrefix(remotePath, "/") {
		remotePath = "/" + remotePath
	}
	if fi, err := sc.Stat(remotePath); err == nil && fi.IsDir() {
		remotePath = path.Join(remotePath, hdr.Filename)
	}
	dst, err := sc.Create(remotePath)
	if err != nil {
		http.Error(w, "创建远程文件失败: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer dst.Close()
	n, err := io.Copy(dst, file)
	if err != nil {
		http.Error(w, "上传中断: "+err.Error(), http.StatusBadGateway)
		return
	}
	log.Printf("[sftp] worker %d upload %s (%d bytes)", wid, remotePath, n)
	writeJSON(w, map[string]interface{}{"ok": true, "path": remotePath, "bytes": n})
}

// handleDownload — ?worker_id=&path= 流式下载 (Content-Disposition 带文件名)。
func handleDownload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "GET only", http.StatusMethodNotAllowed)
		return
	}
	wid, ok := queryUint(r, "worker_id")
	if !ok {
		http.Error(w, "worker_id required", http.StatusBadRequest)
		return
	}
	p := strings.TrimSpace(r.URL.Query().Get("path"))
	if p == "" || !strings.HasPrefix(p, "/") {
		http.Error(w, "path required (absolute)", http.StatusBadRequest)
		return
	}
	sc, cli, err := sftpClient(wid)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	defer sc.Close()
	defer cli.Close()

	fi, err := sc.Stat(p)
	if err != nil {
		http.Error(w, "远程文件不存在: "+err.Error(), http.StatusNotFound)
		return
	}
	if fi.IsDir() {
		http.Error(w, "不支持下载目录 (请压缩后传输)", http.StatusBadRequest)
		return
	}
	src, err := sc.Open(p)
	if err != nil {
		http.Error(w, "打开远程文件失败: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer src.Close()
	w.Header().Set("Content-Disposition",
		`attachment; filename="`+path.Base(p)+`"`)
	w.Header().Set("Content-Type", "application/octet-stream")
	n, err := io.Copy(w, src)
	if err != nil {
		log.Printf("[sftp] worker %d download %s aborted: %v", wid, p, err)
		return
	}
	log.Printf("[sftp] worker %d download %s (%d bytes)", wid, p, n)
}

// ---------- 小工具 ----------

func writeJSON(w http.ResponseWriter, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func decodeJSON(r *http.Request, v interface{}) error {
	return json.NewDecoder(r.Body).Decode(v)
}

func queryUint(r *http.Request, key string) (uint, bool) {
	var n int
	if _, err := fmt.Sscanf(r.URL.Query().Get(key), "%d", &n); err != nil || n <= 0 {
		return 0, false
	}
	return uint(n), true
}

func formUint(r *http.Request, key string) (uint, bool) {
	var n int
	if _, err := fmt.Sscanf(r.FormValue(key), "%d", &n); err != nil || n <= 0 {
		return 0, false
	}
	return uint(n), true
}
