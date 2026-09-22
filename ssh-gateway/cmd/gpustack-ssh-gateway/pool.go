package main

// pool.go — SSH 连接池: 复用已建立的 *ssh.Client, 免掉每请求一次完整
// TCP+SSH 握手 (内网也要 50-200ms, 是补全/目录跟随慢的主因)。
//
// 用法: cli := pooledClient(wid) 拿到的连接 **用完不要 Close** (放回池);
// 连接坏了 (已断开) 时池自动重建。saveCred 换凭据时调 poolInvalidate。
//
// 并发安全: 每节点一把互斥锁串行化建连; 空闲连接由 ssh keepalive 保活。

import (
	"fmt"
	"sync"
	"time"

	"golang.org/x/crypto/ssh"
)

type pooledNode struct {
	mu   sync.Mutex
	cli  *ssh.Client
	gen  int64 // 凭据代数 — 换凭据后旧连接不再复用
	last time.Time
}

type sshPool struct {
	mu    sync.Mutex
	nodes map[uint]*pooledNode
	gen   int64
}

var pool = &sshPool{nodes: map[uint]*pooledNode{}}

// poolBumpGen — 凭据变更时调用: 旧连接全部作废。
func poolBumpGen() {
	pool.mu.Lock()
	pool.gen++
	old := pool.nodes
	pool.nodes = map[uint]*pooledNode{}
	pool.mu.Unlock()
	for _, n := range old {
		n.mu.Lock()
		if n.cli != nil {
			_ = n.cli.Close()
		}
		n.cli = nil
		n.mu.Unlock()
	}
}

func (p *sshPool) node(wid uint) *pooledNode {
	p.mu.Lock()
	defer p.mu.Unlock()
	n, ok := p.nodes[wid]
	if !ok {
		n = &pooledNode{}
		p.nodes[wid] = n
	}
	return n
}

// pooledClient — 取该节点的复用连接; 没有或已断开则重建。
// 返回的连接归池所有, 调用方不得 Close。
func pooledClient(wid uint) (*ssh.Client, error) {
	c := getCred(wid)
	if c == nil {
		return nil, fmt.Errorf("该节点尚未配置 SSH 凭据")
	}
	n := pool.node(wid)
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.cli != nil {
		_, _, err := n.cli.SendRequest("keepalive@openssh.com", true, nil)
		if err == nil {
			n.last = time.Now()
			return n.cli, nil
		}
		// 连接已死: 关掉重建
		_ = n.cli.Close()
		n.cli = nil
	}
	cli, _, err := sshDial(c)
	if err != nil {
		_, msg := classifyErr(err)
		return nil, fmt.Errorf("%s (%s:%d)", msg, c.IP, c.port())
	}
	n.cli = cli
	n.last = time.Now()
	return cli, nil
}

// pooledSession — 复用连接上开一个新 session (exec 用完即关, 连接保留)。
func pooledSession(wid uint) (*ssh.Session, error) {
	cli, err := pooledClient(wid)
	if err != nil {
		return nil, err
	}
	return cli.NewSession()
}
