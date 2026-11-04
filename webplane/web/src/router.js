import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from './views/Dashboard.vue'
import Agents from './views/Agents.vue'
import AgentDetail from './views/AgentDetail.vue'
import Tasks from './views/Tasks.vue'
import TaskNew from './views/TaskNew.vue'
import TaskDetail from './views/TaskDetail.vue'

const routes = [
  { path: '/', name: 'dashboard', component: Dashboard, meta: { title: '概览' } },
  { path: '/agents', name: 'agents', component: Agents, meta: { title: 'Agents' } },
  { path: '/agents/:id', name: 'agent-detail', component: AgentDetail, meta: { title: 'Agent 详情' } },
  { path: '/tasks', name: 'tasks', component: Tasks, meta: { title: '任务' } },
  { path: '/tasks/new', name: 'task-new', component: TaskNew, meta: { title: '新建任务' } },
  { path: '/tasks/:id', name: 'task-detail', component: TaskDetail, meta: { title: '任务详情' } }
]

export default createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 })
})
