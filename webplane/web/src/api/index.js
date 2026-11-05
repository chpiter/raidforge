import client from './client'

export const getHealth = () => client.get('/health')
export const getStats = () => client.get('/stats')

export const getAgents = (params) => client.get('/agents', { params })
export const getAgent = (agentId) => client.get(`/agents/${encodeURIComponent(agentId)}`)
export const deleteAgent = (agentId) =>
  client.post(`/agents/${encodeURIComponent(agentId)}/delete`)
export const getAgentTasks = (agentId, params) =>
  client.get(`/agents/${encodeURIComponent(agentId)}/tasks`, { params })
export const getAgentControllers = (agentId) =>
  client.get(`/agents/${encodeURIComponent(agentId)}/controllers`)
export const getAgentController = (agentId, controllerId) =>
  client.get(
    `/agents/${encodeURIComponent(agentId)}/controllers/${encodeURIComponent(controllerId)}`
  )

export const getTasks = (params) => client.get('/tasks', { params })
export const getTask = (taskId) => client.get(`/tasks/${encodeURIComponent(taskId)}`)
export const createTask = (payload) => client.post('/tasks', payload)
export const createTasks = (payloads) => Promise.all(payloads.map((p) => client.post('/tasks', p)))
export const cancelTask = (taskId, reason) =>
  client.post(`/tasks/${encodeURIComponent(taskId)}/cancel`, { reason })
export const getTaskEvents = (taskId) =>
  client.get(`/tasks/${encodeURIComponent(taskId)}/events`)
export const getTaskTypes = () => client.get('/task-types')

export const evidenceDownloadUrl = (evidenceId) => `/api/cp/evidence/${evidenceId}/download`

export const getSessions = (params) => client.get('/sessions', { params })
export const getSession = (sessionId) =>
  client.get(`/sessions/${encodeURIComponent(sessionId)}`)
export const getSessionEvents = (sessionId, limit = 500) =>
  client.get(`/sessions/${encodeURIComponent(sessionId)}/events`, { params: { limit } })
export const getSessionReport = (sessionId) =>
  client.get(`/sessions/${encodeURIComponent(sessionId)}/report`)
export const getSessionFileText = (sessionId, path) =>
  client.get(
    `/sessions/${encodeURIComponent(sessionId)}/file`,
    { params: { path }, responseType: 'text' }
  )
export const sessionFileUrl = (sessionId, path, download = false) =>
  `/api/cp/sessions/${encodeURIComponent(sessionId)}/file?path=${encodeURIComponent(path)}${download ? '&download=1' : ''}`
export const sessionDownloadUrl = (sessionId) =>
  `/api/cp/sessions/${encodeURIComponent(sessionId)}/download`
