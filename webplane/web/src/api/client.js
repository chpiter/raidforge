import axios from 'axios'

const client = axios.create({
  baseURL: '/api/cp',
  timeout: 20000
})

client.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const data = error.response?.data || {}
    return Promise.reject({
      status: error.response?.status || 0,
      error_code: data.error_code || 'network_error',
      message: data.message || error.message || '请求失败',
      fields: data.fields || []
    })
  }
)

export default client
