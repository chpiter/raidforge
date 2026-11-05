<template>
  <el-container class="shell">
    <el-aside width="220px" class="aside">
      <div class="brand">
        <div class="brand-mark">RF</div>
        <div>
          <div class="brand-name">RaidForge</div>
          <div class="brand-sub">Control Plane</div>
        </div>
      </div>
      <el-menu :default-active="active" class="menu" @select="onSelect">
        <el-menu-item index="/">
          <el-icon><Odometer /></el-icon>
          <span>概览</span>
        </el-menu-item>
        <el-menu-item index="/servers">
          <el-icon><Monitor /></el-icon>
          <span>服务器</span>
        </el-menu-item>
        <el-menu-item index="/tasks">
          <el-icon><List /></el-icon>
          <span>任务</span>
        </el-menu-item>
        <el-menu-item index="/evidence">
          <el-icon><FolderOpened /></el-icon>
          <span>证据与报告</span>
        </el-menu-item>
      </el-menu>
      <div class="aside-foot">
        <el-tag size="small" type="warning" effect="plain">无认证</el-tag>
        <span>仅内网 / VPN 部署</span>
      </div>
    </el-aside>
    <el-container>
      <el-header class="header">
        <div class="crumb">{{ route.meta.title || 'RaidForge Control Plane' }}</div>
        <div class="header-right">
          <el-tag size="small" effect="plain" type="info">webplane</el-tag>
        </div>
      </el-header>
      <el-main class="main">
        <router-view :key="route.fullPath" />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()
const active = computed(() => {
  if (route.path.startsWith('/servers') || route.path.startsWith('/agents')) return '/servers'
  if (route.path.startsWith('/tasks')) return '/tasks'
  if (route.path.startsWith('/evidence')) return '/evidence'
  return '/'
})

const onSelect = (index) => router.push(index)
</script>

<style scoped>
.shell {
  height: 100vh;
}

.aside {
  background: #0f172a;
  color: #e2e8f0;
  display: flex;
  flex-direction: column;
  padding: 18px 12px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 8px 18px;
}

.brand-mark {
  width: 38px;
  height: 38px;
  border-radius: 10px;
  background: linear-gradient(135deg, #2563eb, #38bdf8);
  color: #fff;
  display: grid;
  place-items: center;
  font-weight: 700;
}

.brand-name {
  font-size: 16px;
  font-weight: 700;
  color: #fff;
}

.brand-sub {
  font-size: 12px;
  color: #94a3b8;
}

.menu {
  border-right: none;
  background: transparent;
  --el-menu-text-color: #cbd5e1;
  --el-menu-hover-bg-color: #1e293b;
  --el-menu-active-color: #fff;
}

.menu :deep(.el-menu-item.is-active) {
  background: #1d4ed8;
  border-radius: 8px;
}

.aside-foot {
  margin-top: auto;
  padding: 12px 8px;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #94a3b8;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid var(--rf-border);
}

.crumb {
  font-size: 18px;
  font-weight: 600;
}

.main {
  padding: 20px;
  overflow: auto;
}
</style>
