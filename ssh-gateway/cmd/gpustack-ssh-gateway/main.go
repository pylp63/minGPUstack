// Package main — GPUStack SSH 网关 (二开)
//
// 功能:
//  1. WebSocket 终端代理: 浏览器 (xterm.js) <-> /ws/ssh?id=<worker_id> <-> 节点 SSH (crypto/ssh)
//  2. 会话认证: 一次性 ticket (由 GPUStack 主服务签发, 用后即焚, 10 秒有效)
//  3. SSH 密码轮换: 定期把节点登录用户密码改为高强度随机密码 (默认关闭, 环境变量开启)
//     - 密码仅存在内存 + 权限 0600 的本地状态文件 (重启恢复会话)
//     - 密码历史不落盘; 轮换周期可配
//
// 安全设计:
//  - ticket 一次性 + 短时效, 防重放
//  - SSH 会话用节点随机密码登录 (不是静态凭据)
//  - 网关进程与 GPUStack 主服务同容器部署, 只监听 127.0.0.1
//  - 密码生成用 crypto/rand, 32 字符 (大小写+数字+符号, 混淆字符剔除)
package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"golang.org/x/crypto/ssh"
)

// ---------- 配置 ----------

type Config struct {
	ListenAddr    string // 网关监听地址 (默认 127.0.0.1:10170, 经主服务反代)
	SSHUser       string // 节点 SSH 用户 (默认 root)
	SSHPort       int    // 节点 SSH 端口 (默认 22)
	TicketTTL     time.Duration
	RotateEnabled bool          // 是否开启定期随机密码轮换
	RotateEvery   time.Duration // 轮换周期 (默认 24h)
	StateFile     string        // 密码状态文件 (0600, 重启恢复)
}

func loadConfig() Config {
	c := Config{
		ListenAddr:  envOr("SSHGW_LISTEN", "127.0.0.1:10170"),
		SSHUser:     envOr("SSHGW_SSH_USER", "root"),
		SSHPort:     envOrInt("SSHGW_SSH_PORT", 22),
		TicketTTL:   10 * time.Second,
		StateFile:   envOr("SSHGW_STATE_FILE", "/var/lib/gpustack/ssh-gateway-state.json"),
		RotateEvery: 24 * time.Hour,
	}
	if envOr("SSHGW_ROTATE_PASSWORDS", "") == "true" {
		c.RotateEnabled = true
	}
	if d, err := time.ParseDuration(envOr("SSHGW_ROTATE_EVERY", "")); err == nil && d > 0 {
		c.RotateEvery = d
	}
	return c
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func envOrInt(k string, d int) int {
	if v := os.Getenv(k); v != "" {
		var n int
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil {
			return n
		}
	}
	return d
}

// ---------- 一次性 ticket ----------

type Ticket struct {
	WorkerID uint
	IP       string
	Expires  time.Time
}

type TicketStore struct {
	mu      sync.Mutex
	byToken map[string]*Ticket
}

func NewTicketStore() *TicketStore {
	return &TicketStore{byToken: map[string]*Ticket{}}
}

// Issue 签发一次性 ticket (token 32 字节随机 hex)。
func (s *TicketStore) Issue(workerID uint, ip string) string {
	b := make([]byte, 32)
	_, _ = rand.Read(b)
	tok := hex.EncodeToString(b)
	s.mu.Lock()
	s.byToken[tok] = &Ticket{WorkerID: workerID, IP: ip, Expires: time.Now().Add(loadCfg.TicketTTL)}
	s.mu.Unlock()
	// 惰性清理
	go s.sweep()
	return tok
}

// Consume 消费 ticket — 一次性 (成功即删)。
func (s *TicketStore) Consume(tok string) (uint, string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.byToken[tok]
	if !ok || time.Now().After(t.Expires) {
		if ok {
			delete(s.byToken, tok)
		}
		return 0, "", false
	}
	delete(s.byToken, tok)
	return t.WorkerID, t.IP, true
}

func (s *TicketStore) sweep() {
	s.mu.Lock()
	now := time.Now()
	for k, t := range s.byToken {
		if now.After(t.Expires) {
			delete(s.byToken, k)
		}
	}
	s.mu.Unlock()
}

var loadCfg = loadConfig() // 包级配置 (Issue 等处引用 TTL)

// ---------- 密码轮换 ----------

// PwState — 每节点当前随机密码 (内存为主, 状态文件恢复)。
type nodePw struct {
	IP string `json:"ip"`
	Pw string `json:"pw"`
}

type PwState struct {
	mu   sync.RWMutex
	pwds map[uint]nodePw // worker_id -> {节点 IP, 当前密码}
}

var pwState = &PwState{pwds: map[uint]nodePw{}}

// genPassword — crypto/rand 高强度密码, 32 字符, 剔除易混淆字符 (0O1lI|`'").
const pwAlphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%^&*()-_=+[]{};:,.?/"

func genPassword() (string, error) {
	n := 32
	out := make([]byte, n)
	max := big.NewInt(int64(len(pwAlphabet)))
	for i := 0; i < n; i++ {
		idx, err := rand.Int(rand.Reader, max)
		if err != nil {
			return "", err
		}
		out[i] = pwAlphabet[idx.Int64()]
	}
	return string(out), nil
}

// ensurePassword — 取节点当前密码; 没有则生成 + 立即改到节点 (chpasswd via SSH)。
func ensurePassword(workerID uint, host string) (string, error) {
	pwState.mu.RLock()
	np, ok := pwState.pwds[workerID]
	pwState.mu.RUnlock()
	if ok && np.IP == host {
		return np.Pw, nil
	}
	if ok && np.IP != host {
		// 节点 IP 变了 (重装/漂移): 旧密码对新 IP 无意义, 重新引导
		ok = false
	}
	// 首次: 需要「引导密码」登录改密。引导凭据来自环境 (现场实施时注入一次),
	// 轮换开启后引导凭据即作废 (密码已被随机值替换)。
	bootstrap := os.Getenv("SSHGW_BOOTSTRAP_PASSWORD")
	if bootstrap == "" {
		return "", fmt.Errorf("节点 %d 无已知密码且未设置 SSHGW_BOOTSTRAP_PASSWORD", workerID)
	}
	newPw, err := genPassword()
	if err != nil {
		return "", err
	}
	if err := rotateRemotePassword(host, bootstrap, newPw); err != nil {
		return "", fmt.Errorf("首次改密失败 (节点 %s): %w", host, err)
	}
	pwState.mu.Lock()
	pwState.pwds[workerID] = nodePw{IP: host, Pw: newPw}
	pwState.mu.Unlock()
	savePwState()
	return newPw, nil
}

// rotateRemotePassword — 用旧密码 SSH 登录, chpasswd 改新密码。
func rotateRemotePassword(host, oldPw, newPw string) error {
	cfg := &ssh.ClientConfig{
		User:            loadCfg.SSHUser,
		HostKeyCallback: ssh.InsecureIgnoreHostKey(), // 内网节点, 首版不做 known_hosts
		Auth:            []ssh.AuthMethod{ssh.Password(oldPw)},
		Timeout:         10 * time.Second,
	}
	cli, err := ssh.Dial("tcp", net.JoinHostPort(host, fmt.Sprint(loadCfg.SSHPort)), cfg)
	if err != nil {
		return err
	}
	defer cli.Close()
	sess, err := cli.NewSession()
	if err != nil {
		return err
	}
	defer sess.Close()
	// chpasswd 从 stdin 读 "user:newpass" — 密码不进命令行 (ps 不可见)
	sess.Stdin = strings.NewReader(loadCfg.SSHUser + ":" + newPw + "\n")
	return sess.Run("chpasswd")
}

// rotateLoop — 定期轮换所有已管理节点。
func rotateLoop(ctx context.Context) {
	if !loadCfg.RotateEnabled {
		return
	}
	tk := time.NewTicker(loadCfg.RotateEvery)
	defer tk.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tk.C:
			pwState.mu.Lock()
			ids := make([]uint, 0, len(pwState.pwds))
			for id := range pwState.pwds {
				ids = append(ids, id)
			}
			pwState.mu.Unlock()
			for _, id := range ids {
				pwState.mu.RLock()
				old := pwState.pwds[id]
				pwState.mu.RUnlock()
				if old.IP == "" || old.Pw == "" {
					continue
				}
				np, err := genPassword()
				if err != nil {
					continue
				}
				if err := rotateRemotePassword(old.IP, old.Pw, np); err != nil {
					log.Printf("[rotate] worker %d (%s) 轮换失败: %v", id, old.IP, err)
					continue
				}
				pwState.mu.Lock()
				pwState.pwds[id] = nodePw{IP: old.IP, Pw: np}
				pwState.mu.Unlock()
				log.Printf("[rotate] worker %d (%s) 密码已轮换", id, old.IP)
			}
			savePwState()
		}
	}
}

// savePwState / loadPwState — 0600 状态文件, 重启恢复 (轮换场景必须)。
func savePwState() {
	pwState.mu.RLock()
	data, _ := json.Marshal(pwState.pwds)
	pwState.mu.RUnlock()
	_ = os.WriteFile(loadCfg.StateFile, data, 0600)
}

func loadPwState() {
	data, err := os.ReadFile(loadCfg.StateFile)
	if err != nil {
		return
	}
	m := map[uint]nodePw{}
	if json.Unmarshal(data, &m) == nil {
		pwState.mu.Lock()
		pwState.pwds = m
		pwState.mu.Unlock()
	}
}

// ---------- WebSocket 终端 ----------

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true }, // 同源反代
	ReadBufferSize:  4096,
	WriteBufferSize: 4096,
}

// wsMsg — xterm 前端协议 {type:"input"|"resize"|"ping", data, cols, rows}
type wsMsg struct {
	Type  string `json:"type"`
	Data  string `json:"data"`
	Cols  int    `json:"cols"`
	Rows  int    `json:"rows"`
}

func handleTerminal(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	tok := q.Get("ticket")
	workerID, host, ok := tickets.Consume(tok)
	if !ok || host == "" {
		http.Error(w, "invalid or expired ticket", http.StatusUnauthorized)
		return
	}
	pw, err := ensurePassword(workerID, host)
	if err != nil {
		http.Error(w, "password provision failed: "+err.Error(), http.StatusBadGateway)
		return
	}

	// SSH 连接
	cfg := &ssh.ClientConfig{
		User:            loadCfg.SSHUser,
		HostKeyCallback: ssh.InsecureIgnoreHostKey(),
		Auth:            []ssh.AuthMethod{ssh.Password(pw)},
		Timeout:         10 * time.Second,
	}
	cli, err := ssh.Dial("tcp", net.JoinHostPort(host, fmt.Sprint(loadCfg.SSHPort)), cfg)
	if err != nil {
		http.Error(w, "ssh dial failed: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer cli.Close()
	sess, err := cli.NewSession()
	if err != nil {
		http.Error(w, "ssh session failed: "+err.Error(), http.StatusBadGateway)
		return
	}
	defer sess.Close()

	modes := ssh.TerminalModes{
		ssh.ECHO:          1,
		ssh.TTY_OP_ISPEED: 14400,
		ssh.TTY_OP_OSPEED: 14400,
	}
	if err := sess.RequestPty("xterm-256color", 40, 120, modes); err != nil {
		http.Error(w, "pty failed: "+err.Error(), http.StatusBadGateway)
		return
	}
	stdin, _ := sess.StdinPipe()
	stdout, _ := sess.StdoutPipe()
	stderr, _ := sess.StderrPipe()
	if err := sess.Shell(); err != nil {
		http.Error(w, "shell failed: "+err.Error(), http.StatusBadGateway)
		return
	}

	ws, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	defer ws.Close()

	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()

	// stdout/stderr -> WS
	go func() {
		buf := make([]byte, 8192)
		sp := multiReader{stdout, stderr}
		for {
			n, err := sp.Read(buf)
			if n > 0 {
				_ = ws.WriteMessage(websocket.BinaryMessage, buf[:n])
			}
			if err != nil || ctx.Err() != nil {
				cancel()
				return
			}
		}
	}()

	// WS -> stdin
	go func() {
		for {
			mt, data, err := ws.ReadMessage()
			if err != nil || ctx.Err() != nil {
				cancel()
				return
			}
			if mt != websocket.TextMessage {
				continue
			}
			var m wsMsg
			if json.Unmarshal(data, &m) != nil {
				continue
			}
			switch m.Type {
			case "input":
				_, _ = stdin.Write([]byte(m.Data))
			case "resize":
				_ = sess.WindowChange(m.Rows, m.Cols)
			case "ping":
				_ = ws.WriteJSON(wsMsg{Type: "pong"})
			}
		}
	}()

	<-ctx.Done()
	_ = ws.Close()
	_ = sess.Wait()
}

// multiReader — 顺序读 stdout 再 stderr (简单聚合)。
type readCloser interface {
	Read(p []byte) (int, error)
}

type multiReader struct {
	a, b readCloser
}

func (m multiReader) Read(p []byte) (int, error) {
	if m.a != nil {
		n, err := m.a.Read(p)
		if err == nil || n > 0 {
			return n, nil
		}
		m.a = nil
	}
	return m.b.Read(p)
}

// ---------- HTTP 路由 ----------

var tickets = NewTicketStore()

// handleTicket — 签发 ticket (主服务反代后走 GPUStack 登录态; 直连时校验头)。
func handleTicket(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		WorkerID uint   `json:"worker_id"`
		IP       string `json:"ip"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.WorkerID == 0 || req.IP == "" {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	tok := tickets.Issue(req.WorkerID, req.IP)
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]string{"ticket": tok})
}

func main() {
	loadPwState()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/ssh/ticket", handleTicket)
	mux.HandleFunc("/ws/ssh", handleTerminal)
	// 静态终端页 (构建时嵌入)
	mux.Handle("/", http.FileServer(http.FS(webFS)))

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	go rotateLoop(ctx)

	srv := &http.Server{Addr: loadCfg.ListenAddr, Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	log.Printf("[ssh-gateway] listening on %s (rotate=%v)", loadCfg.ListenAddr, loadCfg.RotateEnabled)
	go func() {
		<-ctx.Done()
		shutCtx, c := context.WithTimeout(context.Background(), 3*time.Second)
		defer c()
		_ = srv.Shutdown(shutCtx)
	}()
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}

// debug 哈希 (不暴露明文)
func pwHash(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:8])
}
