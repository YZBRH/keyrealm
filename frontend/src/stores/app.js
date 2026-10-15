import { defineStore } from 'pinia'
import { api } from '@/api'
import { useVaultStore } from './vault'
import { useMcpStore } from './mcp'

let bootstrapPromise = null

// 活动上报节流：后端据此判定空闲，无需每次鼠标移动都打一趟桥
const ACTIVITY_REPORT_INTERVAL = 30 * 1000
let lastActivityReport = 0

export const useAppStore = defineStore('app', {
  state: () => ({
    bootstrapped: false,
    initialized: false,
    unlocked: false,
    tray: {
      mcp_running: false,
      mcp_port: 8765,
      active_tokens: 0,
      is_locked: true,
    },
  }),

  actions: {
    /** 读取初始化/解锁状态。可安全重复调用（并发去重 + 结果缓存）。
     *
     * **失败会抛出**，由调用方决定重试——这点很关键：以前这里把异常吞掉并置
     * `initialized = false`，等于把「读不到」说成「没注册」，用户会被扔到注册页面
     * （看起来就像数据丢了），而且因为下面把 `bootstrapped` 置了真，之后再也不会重试。
     */
    async bootstrap(force = false) {
      if (this.bootstrapped && !force) return
      if (bootstrapPromise && !force) return bootstrapPromise

      const task = (async () => {
        try {
          const initResult = await api.isInitialized()
          this.initialized = !!(initResult && initResult.initialized)
          if (this.initialized) {
            const unlockedResult = await api.isUnlocked()
            this.unlocked = !!(unlockedResult && unlockedResult.unlocked)
          } else {
            this.unlocked = false
          }
          this.bootstrapped = true   // 只有**成功**才算引导完成；失败时留 false，下次调用会重试
        } catch (err) {
          // 前端控制台用户看不到——把真实原因回传到后端日志（best-effort，这条本身失败就算了）。
          api.logFrontendError(
            `读取初始化状态失败：${err && err.message ? err.message : String(err)}`
          ).catch(() => {})
          throw err
        }
      })()

      bootstrapPromise = task
      try {
        await task
      } finally {
        bootstrapPromise = null
      }
    },

    async setupMasterPassword(password) {
      const result = await api.setupMasterPassword(password)
      this.initialized = true
      this.unlocked = true
      await this.refreshTray()
      return result
    },

    async unlock(password) {
      const result = await api.unlock(password)
      this.unlocked = true
      await this.refreshTray()
      return result
    },

    /** 锁定后的前端收尾：清空内存明文、标记托盘为已锁。**不**调后端。
     *  既用于用户点「立即锁定」，也用于后端广播 vault:locked（托盘/空闲自动锁）时。 */
    applyLocked() {
      this.unlocked = false
      this.tray.is_locked = true
      useVaultStore().clear()
      useMcpStore().clear()
    },

    /** 锁定保险库并清空内存中的明文数据。 */
    async lock() {
      try {
        await api.lockVault()
      } finally {
        this.applyLocked()
      }
    },

    /** 上报用户活动（节流至 30s 一次），供后端「空闲自动锁定」计时。
     *  未解锁时不上报——锁着的时候后端也不该攒活动。失败静默：这只是个计时信号。 */
    reportActivity() {
      if (!this.unlocked) return
      const now = Date.now()
      if (now - lastActivityReport < ACTIVITY_REPORT_INTERVAL) return
      lastActivityReport = now
      try {
        const p = api.touchActivity()
        if (p && typeof p.catch === 'function') p.catch(() => {})
      } catch {
        // 桥未就绪等情况：忽略
      }
    },

    async refreshTray() {
      try {
        const status = await api.getTrayStatus()
        if (status) {
          this.tray = {
            mcp_running: !!status.mcp_running,
            mcp_port: status.mcp_port ?? 8765,
            active_tokens: status.active_tokens ?? 0,
            is_locked: !!status.is_locked,
          }
        }
      } catch {
        // 托盘状态为辅助信息，失败不阻塞主流程
      }
    },

    async minimizeToTray() {
      const result = await api.minimizeToTray()
      await this.refreshTray()
      return result
    },
  },
})