import { defineStore } from 'pinia'
import { api } from '@/api'

/**
 * 代理注册表状态 + 「测试代理」的进行态与结果。
 *
 * **测试状态为什么放 store**：`Settings.vue` 里的 `proxyTesting` / `proxyTest` 原本是页面
 * 局部 ref，而 `<router-view>` 没有 keep-alive —— 切到别的功能栏组件就卸载了，后端其实
 * 已经跑完，结果却写进已销毁的实例里，用户看到的现象就是「测试被中断」。
 * 放进 store 后切页不丢，与批量验证（`stores/vault.js` 的 `batch`）是同一套做法。
 */
export const useProxyStore = defineStore('proxy', {
  state: () => ({
    items: [],
    globalId: null,
    globalUrl: '',
    globalDisplay: '',
    testing: false,
    testingId: null,      // 正在测哪个代理；null 且 testing=true 表示在测「全局当前生效的」
    result: null,
  }),

  actions: {
    async fetch() {
      const res = await api.listProxies()
      this.items = (res && res.items) || []
      this.globalId = (res && res.global_id) ?? null
      this.globalUrl = (res && res.global_url) || ''
      this.globalDisplay = (res && res.global_display) || this.globalUrl
      return this.items
    },

    async create(name, url) {
      const res = await api.createProxy(name, url)
      if (res && res.ok === false) throw new Error(res.message || '新增失败')
      await this.fetch()
      return res
    },

    async update(id, name = null, url = null) {
      const res = await api.updateProxy(id, name, url)
      if (res && res.ok === false) throw new Error(res.message || '修改失败')
      await this.fetch()
      return res
    },

    async remove(id) {
      const res = await api.deleteProxy(id)
      if (res && res.ok === false) throw new Error(res.message || '删除失败')
      await this.fetch()
      return res
    },

    async createChain(name, memberIds) {
      const res = await api.createProxyChain(name, memberIds)
      if (res && res.ok === false) throw new Error(res.message || '新增代理链失败')
      await this.fetch()
      return res
    },

    async updateChain(id, name, memberIds) {
      const res = await api.updateProxyChain(id, name, memberIds)
      if (res && res.ok === false) throw new Error(res.message || '修改代理链失败')
      await this.fetch()
      return res
    },

    async removeChain(id) {
      const res = await api.deleteProxyChain(id)
      if (res && res.ok === false) throw new Error(res.message || '删除代理链失败')
      await this.fetch()
      return res
    },

    async setGlobal(id = null) {
      const res = await api.setGlobalProxy(id)
      if (res && res.ok === false) throw new Error(res.message || '设置失败')
      await this.fetch()
      return res
    },

    /** 测试某个代理；`id = null` 表示测当前全局生效的那个 */
    async runTest(id = null) {
      this.testing = true
      this.testingId = id
      this.result = null
      try {
        const res = await api.testProxyId(id)
        this.result = res
        return res
      } finally {
        this.testing = false
        this.testingId = null
      }
    },

    clearResult() {
      this.result = null
    },
  },
})
