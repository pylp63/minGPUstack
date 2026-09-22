// Package main — GPUStack SSH 网关 (二开)
//
// 功能:
//  1. WebSocket 终端代理: 浏览器 (xterm.js) <-> /ws/ssh?ticket=... <-> 节点 SSH (crypto/ssh)
//  2. 会话认证: 一次性 ticket (GPUStack 主服务签发, 用后即焚, 10 秒有效)
//  3. 节点凭据管理 (自动发现节点的配套设计): 集群节点通过 worker 注册/K8S
//     自动发现, 系统不预知 SSH 凭据 — 首次连接时前端弹窗录入
//     (POST /api/ssh/credentials), 网关先实际登录验证, 通过才保存。
//  4. 连接失败分类: 端口不通 (unreachable) / 用户名密码错误 (auth_failed) /
//     未配置凭据 (no_credentials) / 其它 SSH 错误 (ssh_error), 以
//     {type:"error",code,message} JSON 消息下发, 前端按类型弹窗。
//  5. SSH 密码轮换 (可选, SSHGW_ROTATE_PASSWORDS=true): 定期把节点登录
//     密码改为高强度随机密码。
//
// 安全设计:
//   - ticket 一次性 + 短时效, 防重放
//   - 凭据仅存内存 + 0600 状态文件 (容器卷内, 重启恢复); 密码经 stdin 传给
//     chpasswd / 只用于 crypto/ssh 认证, 不进命令行参数与日志
//   - 网关只监听 127.0.0.1, 必须经 GPUStack 主服务 (登录态) 才能触达
package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
	"golang.org/x/crypto/ssh"
)

// ---------- 配置 ----------

type Config struct {
	ListenAddr    string
	SSHUser       string // 弹窗默认用户名 (默认 root)
	SSHPort       int    // 弹窗默认端口 (默认 22)
	TicketTTL     time.Duration
	RotateEnabled bool
	RotateEvery   time.Duration
	StateFile     string
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

var loadCfg = loadConfig()

// ---------- 一次性 ticket ----------

type Ticket struct {
	WorkerID uint
	IP       string
	User     string // 多凭据时前端选择的用户名 (空 = 默认第一套)
	Expires  time.Time
}

type TicketStore struct {
	mu      sync.Mutex
	byToken map[string]*Ticket
}

func NewTicketStore() *TicketStore {
	return &TicketStore{byToken: map[string]*Ticket{}}
}

func (s *TicketStore) Issue(workerID uint, ip string, user string) string {
	b := make([]byte, 32)
	_, _ = rand.Read(b)
	tok := fmt.Sprintf("%x", b)
	s.mu.Lock()
	s.byToken[tok] = &Ticket{WorkerID: workerID, IP: ip, User: user,
		Expires: time.Now().Add(loadCfg.TicketTTL)}
	s.mu.Unlock()
	go s.sweep()
	return tok
}

// Consume 消费 ticket — 一次性 (成功即删)。
func (s *TicketStore) Consume(tok string) (uint, string, string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	t, ok := s.byToken[tok]
	if !ok || time.Now().After(t.Expires) {
		if ok {
			delete(s.byToken, tok)
		}
		return 0, "", "", false
	}
	delete(s.byToken, tok)
	return t.WorkerID, t.IP, t.User, true
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

var tickets = NewTicketStore()

// ---------- 节点凭据 ----------

// nodeCred — 每节点 SSH 凭据 (弹窗录入 / 轮换写入)。
// 旧状态文件 (只有 ip/pw) 可兼容反序列化: User/Port 为零值时回落默认。
// HostKey: 首次连接时记录的服务器指纹 (base64)。重装系统后指纹变化,
// 连接时报 host_key_changed, 前端提示用户确认 (删除旧记录重新验证)。
type nodeCred struct {
	IP        string `json:"ip"`
	User      string `json:"user"`
	Pw        string `json:"pw"`
	Port      int    `json:"port"`
	HostKey   string `json:"host_key,omitempty"`
	RotatedAt string `json:"rotated_at,omitempty"`
}

func (c *nodeCred) user() string {
	if c.User == "" {
		return loadCfg.SSHUser
	}
	return c.User
}

func (c *nodeCred) port() int {
	if c.Port == 0 {
		return loadCfg.SSHPort
	}
	return c.Port
}

type CredState struct {
	mu    sync.RWMutex
	creds map[uint][]nodeCred // 每节点可存多套凭据 (第一套 = 默认/上次用的)
}

var credState = &CredState{creds: map[uint][]nodeCred{}}

func getCred(workerID uint) *nodeCred {
	l := listCreds(workerID)
	if len(l) == 0 {
		return nil
	}
	return &l[0]
}

// listCreds — 该节点全部凭据 (默认在首位)。前端选择窗口用。
func listCreds(workerID uint) []nodeCred {
	credState.mu.RLock()
	defer credState.mu.RUnlock()
	l, ok := credState.creds[workerID]
	if !ok {
		return nil
	}
	out := make([]nodeCred, len(l))
	copy(out, l)
	return out
}

// pickCred — 按用户名选凭据 (选择窗口场景)。找不到回退默认第一套。
func pickCred(workerID uint, user string) *nodeCred {
	l := listCreds(workerID)
	if len(l) == 0 {
		return nil
	}
	for i := range l {
		if user != "" && l[i].User == user {
			c := l[i]
			return &c
		}
	}
	c := l[0]
	return &c
}

// saveCred — 保存凭据: 同 IP+用户名覆盖, 否则追加; 把目标移到首位 (设为默认)。
func saveCred(workerID uint, c nodeCred) {
	credState.mu.Lock()
	l := credState.creds[workerID]
	for i := range l {
		if l[i].User == c.User && l[i].IP == c.IP {
			// 覆盖已有同用户凭据, 移到首位 (设为默认)
			c.RotatedAt = l[i].RotatedAt // 保留轮换时间戳
			rest := append([]nodeCred{c}, l[:i]...)
			rest = append(rest, l[i+1:]...)
			credState.creds[workerID] = rest
			credState.mu.Unlock()
			poolBumpGen()
			saveState()
			return
		}
	}
	// 新用户: 追加到首位
	credState.creds[workerID] = append([]nodeCred{c}, l...)
	credState.mu.Unlock()
	poolBumpGen()
	saveState()
}

// genPassword — crypto/rand 高强度密码, 32 字符, 剔除易混淆字符。
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

// errHostKeyChanged — 服务器指纹变化 (重装系统等), 前端据此提示确认。
var errHostKeyChanged = errors.New("host key changed")

// sshDial — 用凭据建立 SSH 连接。
// 指纹校验在 KEX 阶段 (HostKeyCallback, 认证之前) 做: 服务器重装后
// 即使密码也对, 也必须先报 host_key_changed, 而不是连上再比。
// 首次连接 (c.HostKey 为空): 接受并记录指纹, 随连接返回。
func sshDial(c *nodeCred) (*ssh.Client, string, error) {
	var gotKey string
	cfg := &ssh.ClientConfig{
		User: c.user(),
		HostKeyCallback: func(hostname string, remote net.Addr, key ssh.PublicKey) error {
			gotKey = ssh.FingerprintSHA256(key)
			if c.HostKey != "" && gotKey != c.HostKey {
				return errHostKeyChanged
			}
			return nil
		},
		Auth:    []ssh.AuthMethod{ssh.Password(c.Pw)},
		Timeout: 10 * time.Second,
	}
	cli, err := ssh.Dial("tcp", net.JoinHostPort(c.IP, fmt.Sprint(c.port())), cfg)
	if err != nil {
		if errors.Is(err, errHostKeyChanged) {
			return nil, gotKey, errHostKeyChanged
		}
		return nil, "", err
	}
	return cli, gotKey, nil
}

// classifyErr — 连接失败分类: 端口不通 / 认证失败 / 其它。
// 前端按 code 弹窗 (unreachable 提示检查 sshd/端口/防火墙,
// auth_failed 提示重新录入密码)。
func classifyErr(err error) (code, message string) {
	if err == nil {
		return "", ""
	}
	if errors.Is(err, errHostKeyChanged) {
		return "host_key_changed", "服务器指纹已变化 (可能重装系统)"
	}
	var opErr *net.OpError
	if errors.As(err, &opErr) {
		return "unreachable", fmt.Sprintf("SSH 端口不通或网络不可达 (%v)", opErr.Err)
	}
	s := err.Error()
	if strings.Contains(s, "unable to authenticate") ||
		strings.Contains(s, "authentication failed") {
		return "auth_failed", "用户名或密码错误"
	}
	if strings.Contains(s, "i/o timeout") || strings.Contains(s, "no route to host") {
		return "unreachable", "SSH 端口不通或网络不可达 (超时)"
	}
	return "ssh_error", s
}

// rotateRemotePassword — 用旧密码登录, chpasswd 改新密码 (密码走 stdin, 不进 ps)。
func rotateRemotePassword(c *nodeCred, newPw string) error {
	cli, _, err := sshDial(c)
	if err != nil {
		return err
	}
	defer cli.Close()
	sess, err := cli.NewSession()
	if err != nil {
		return err
	}
	defer sess.Close()
	sess.Stdin = strings.NewReader(c.user() + ":" + newPw + "\n")
	return sess.Run("chpasswd")
}

// rotateLoop — 兼容保留: 环境变量 SSHGW_ROTATE_PASSWORDS 开关已迁移到
// rotate-config API (页面设置); checker 每 30s 读取配置评估到期。
func rotateLoop(ctx context.Context) {
	if !loadCfg.RotateEnabled {
		return // 默认关; 页面 rotate-config 可随时开启 (与 env 无关)
	}
	rc := readRotate()
	if !rc.Enabled {
		writeRotate(RotateConfig{Enabled: true, Days: rc.Days})
	}
}

// saveState / loadState — 0600 状态文件, 重启恢复。
// 文件是多种 key 的混合体: 数字 workerID key (凭据) + "_global_cred"。
// saveState 只序列化 creds 会抹掉 _global_cred — 必须先读旧文件、保留
// 非 workerID key, 再合并写入。
func saveState() {
	credState.mu.RLock()
	credsData, _ := json.Marshal(credState.creds)
	credState.mu.RUnlock()

	// 保留旧文件里的非凭据 key (_global_cred 等)
	preserved := map[string]json.RawMessage{}
	if old, err := os.ReadFile(loadCfg.StateFile); err == nil {
		var raw map[string]json.RawMessage
		if json.Unmarshal(old, &raw) == nil {
			for k, v := range raw {
				if k == "_global_cred" {
					preserved[k] = v
				}
			}
		}
	}
	var merged map[string]json.RawMessage
	if json.Unmarshal(credsData, &merged) == nil {
		for k, v := range preserved {
			merged[k] = v
		}
		out, _ := json.Marshal(merged)
		_ = os.WriteFile(loadCfg.StateFile, out, 0600)
	} else {
		_ = os.WriteFile(loadCfg.StateFile, credsData, 0600)
	}
}

func loadState() {
	data, err := os.ReadFile(loadCfg.StateFile)
	if err != nil {
		return
	}
	// 文件里可能混有 "_global_cred" 等 string key (uint 解析会整体失败),
	// 先按 string key 拆开, 只把数字 key 的值解析为凭据。
	raw := map[string]json.RawMessage{}
	if json.Unmarshal(data, &raw) != nil {
		return
	}
	// 兼容两种凭据值格式:
	//   旧: {"<wid>": {ip/user/pw/...}}        (单值)
	//   新: {"<wid>": [{...}, {...}]}          (多凭据 slice)
	creds := map[uint][]nodeCred{}
	for k, v := range raw {
		wid, err := strconv.ParseUint(k, 10, 64)
		if err != nil {
			continue // "_global_cred" 等非数字 key
		}
		var multi []nodeCred
		if json.Unmarshal(v, &multi) == nil && len(multi) > 0 {
			creds[uint(wid)] = multi
			continue
		}
		var single nodeCred
		if json.Unmarshal(v, &single) == nil && single.Pw != "" {
			creds[uint(wid)] = []nodeCred{single}
		}
	}
	credState.mu.Lock()
	credState.creds = creds
	credState.mu.Unlock()
}

// ---------- WebSocket 终端 ----------

var upgrader = websocket.Upgrader{
	CheckOrigin:     func(r *http.Request) bool { return true },
	ReadBufferSize:  4096,
	WriteBufferSize: 4096,
}

// wsMsg — xterm 前端协议 (input/resize/ping) + 网关下行 (pong/error)。
type wsMsg struct {
	Type string `json:"type"`
	Code string `json:"code,omitempty"`
	Data string `json:"data,omitempty"`
	Cols int    `json:"cols,omitempty"`
	Rows int    `json:"rows,omitempty"`
}

func sendErr(ws *websocket.Conn, code, msg string) {
	_ = ws.WriteJSON(wsMsg{Type: "error", Code: code, Data: msg})
	_ = ws.WriteControl(websocket.CloseMessage,
		websocket.FormatCloseMessage(1011, code), time.Now().Add(time.Second))
}

func handleTerminal(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	workerID, host, wantUser, ok := tickets.Consume(q.Get("ticket"))
	if !ok || host == "" {
		http.Error(w, "invalid or expired ticket", http.StatusUnauthorized)
		return
	}

	// 先升级 WebSocket: 凭据/连接类失败不作为 HTTP 错误返回 (那样前端
	// 只能看到「连接已断开」), 而是升级后以 {type:"error"} 消息下发,
	// 前端按 code 弹窗 (区分端口不通 / 密码错误 / 未配置凭据)。
	ws, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	defer ws.Close()

	// 多凭据: 前端选择窗口传来的用户名 → 从该节点凭据列表里选对应一套;
	// 未指定 (单凭据/旧前端) 用默认第一套。
	creds := pickCred(workerID, wantUser)
	if creds == nil {
		// 二开: 无节点级凭据时尝试「通用凭据」(一批服务器的统一账号) 自动派生
		gc, gerr := applyGlobalCred(workerID, host)
		if gerr != nil {
			sendErr(ws, "no_credentials",
				"该节点为自动发现, 尚未保存 SSH 登录凭据")
			return
		}
		creds = gc
	}
	creds.IP = host // ticket 携带的是 API 侧最新可达地址

	cli, gotKey, err := sshDial(creds)
	if err != nil {
		// 服务器指纹变化 (重装系统等): 单独错误码, 前端弹窗让用户
		// 确认是否信任新指纹 (信任则删除旧记录 + 用新 key 重新验证保存)。
		if errors.Is(err, errHostKeyChanged) {
			sendErr(ws, "host_key_changed",
				fmt.Sprintf("服务器指纹已变化 (目标 %s:%d, 可能重装系统)。"+
					"旧指纹: %s, 新指纹: %s", creds.IP, creds.port(),
					creds.HostKey, gotKey))
			return
		}
		code, msg := classifyErr(err)
		sendErr(ws, code, fmt.Sprintf("%s (目标 %s:%d)", msg, creds.IP, creds.port()))
		return
	}
	// 首次连接: 记录服务器指纹 (下次连接据此校验重装)
	if creds.HostKey == "" {
		creds.HostKey = gotKey
		saveCred(workerID, *creds)
	}
	defer cli.Close()

	sess, err := cli.NewSession()
	if err != nil {
		sendErr(ws, "ssh_error", "会话创建失败: "+err.Error())
		return
	}
	defer sess.Close()

	modes := ssh.TerminalModes{
		ssh.ECHO:          1,
		ssh.TTY_OP_ISPEED: 14400,
		ssh.TTY_OP_OSPEED: 14400,
	}
	if err := sess.RequestPty("xterm-256color", 40, 120, modes); err != nil {
		sendErr(ws, "ssh_error", "PTY 申请失败: "+err.Error())
		return
	}
	stdin, _ := sess.StdinPipe()
	stdout, _ := sess.StdoutPipe()
	stderr, _ := sess.StderrPipe()
	if err := sess.Shell(); err != nil {
		sendErr(ws, "ssh_error", "Shell 启动失败: "+err.Error())
		return
	}

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
type reader interface {
	Read(p []byte) (int, error)
}

type multiReader struct {
	a, b reader
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

// handleTicket — 签发一次性 ticket (由 GPUStack 主服务反代调用,
// 登录态与节点可见性在主服务侧校验)。
func handleTicket(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		WorkerID uint   `json:"worker_id"`
		IP       string `json:"ip"`
		Username string `json:"username"` // 多凭据: 前端选择窗口指定的用户
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.WorkerID == 0 || req.IP == "" {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	tok := tickets.Issue(req.WorkerID, req.IP, req.Username)
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]string{"ticket": tok})
}

type credResp struct {
	OK      bool   `json:"ok"`
	Code    string `json:"code,omitempty"`
	Message string `json:"message,omitempty"`
}

// handleCredentials — 前端弹窗「验证并连接」: 先实际 SSH 登录验证,
// 通过才保存凭据; 失败按原因分类返回 (前端弹窗提示)。
func handleCredentials(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		WorkerID uint   `json:"worker_id"`
		IP       string `json:"ip"`
		Username string `json:"username"`
		Password string `json:"password"`
		Port     int    `json:"port"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil ||
		req.WorkerID == 0 || req.IP == "" || req.Username == "" || req.Password == "" {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	c := nodeCred{IP: req.IP, User: req.Username, Pw: req.Password, Port: req.Port}
	if c.Port == 0 {
		c.Port = loadCfg.SSHPort
	}
	w.Header().Set("Content-Type", "application/json")
	cli, gotKey, err := sshDial(&c)
	if err != nil {
		// host key 变化: 验证阶段也提示 (重装后旧凭据已失效)
		if errors.Is(err, errHostKeyChanged) {
			_ = json.NewEncoder(w).Encode(credResp{
				OK: false, Code: "host_key_changed",
				Message: fmt.Sprintf("服务器指纹已变化 (可能重装系统)。旧指纹: %s, 新指纹: %s", c.HostKey, gotKey),
			})
			return
		}
		code, msg := classifyErr(err)
		_ = json.NewEncoder(w).Encode(credResp{OK: false, Code: code, Message: msg})
		return
	}
	cli.Close()
	// 首次验证: 记录指纹 (下次据此检测重装)
	if c.HostKey == "" {
		c.HostKey = gotKey
	}
	saveCred(req.WorkerID, c)
	log.Printf("[credentials] worker %d (%s@%s:%d) 凭据已验证并保存",
		req.WorkerID, c.User, c.IP, c.Port)
	_ = json.NewEncoder(w).Encode(credResp{OK: true})
}

func main() {
	loadState()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/ssh/ticket", handleTicket)
	mux.HandleFunc("/api/ssh/credentials", handleCredentials)
	mux.HandleFunc("/api/ssh/cred/list", handleCredList)
	mux.HandleFunc("/api/ssh/cred/delete", handleCredDelete)
	mux.HandleFunc("/api/ssh/rotate-config", handleRotateConfig)
	mux.HandleFunc("/api/ssh/rotate-now", handleRotateNow)
	mux.HandleFunc("/api/ssh/upload", handleUpload)
	mux.HandleFunc("/api/ssh/download", handleDownload)
	mux.HandleFunc("/api/ssh/ls", handleLs)
	mux.HandleFunc("/api/ssh/cwd", handleCwd)
	mux.HandleFunc("/api/ssh/complete", handleComplete)
	mux.HandleFunc("/api/ssh/global-cred", handleGlobalCred)
	mux.HandleFunc("/ws/ssh", handleTerminal)
	mux.Handle("/", http.FileServer(http.FS(webFS)))

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	rotateLoop(ctx) // env 兼容: SSHGW_ROTATE_PASSWORDS=true 时初始化配置
	stop := make(chan struct{})
	go rotateChecker(stop)
	defer close(stop)

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
