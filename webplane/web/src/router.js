import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from './views/Dashboard.vue'
import Servers from './views/Servers.vue'
import ServerDetail from './views/ServerDetail.vue'
import ControllerDetail from './views/ControllerDetail.vue'
import Tasks from './views/Tasks.vue'
import TaskNew from './views/TaskNew.vue'
import TaskDetail from './views/TaskDetail.vue'

const routes = [
  { path: '/', name: 'dashboard', component: Dashboard, meta: { title: '概览' } },
  { path: '/servers', name: 'servers', component: Servers, meta: { title: '服务器' } },
  {
    path: '/servers/:agentId',
    name: 'server-detail',
    component: ServerDetail,
    meta: { title: '服务器详情' }
  },
  {
    path: '/servers/:agentId/controllers/:controllerId',
    name: 'controller-detail',
    component: ControllerDetail,
    meta: { title: '阵列卡详情' }
  },
  { path: '/agents', redirect: '/servers' },
  { path: '/agents/:id', redirect: (to) => `/servers/${to.params.id}` },
  { path: '/tasks', name: 'tasks', component: Tasks, meta: { title: '任务' } },
  { path: '/tasks/new', name: 'task-new', component: TaskNew, meta: { title: '新建任务' } },
  { path: '/tasks/:id', name: 'task-detail', component: TaskDetail, meta: { title: '任务详情' } }
]

export default createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 })
})
