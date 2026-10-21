const fastify = require('fastify')({ logger: false })
const logview = require('./logs')

async function start() {
  if (!process.env.SESSION_TOKEN) {
    throw new Error('SESSION_TOKEN must be set')
  }

  await fastify.register(require('@fastify/express'))

  // All /console routes require the session token; the middleware runs first.
  fastify.use('/console', (req, res, next) => {
    if (req.headers['x-session-token'] !== process.env.SESSION_TOKEN) {
      res.statusCode = 401
      res.end('Unauthorized')
      return
    }
    next()
  })

  fastify.get('/', async (request, reply) => {
    return { message: 'Welcome to the console service' }
  })

  fastify.get('/status', async (request, reply) => {
    return { status: 'ok' }
  })

  fastify.get('/console', async (request, reply) => {
    return { message: 'Console index' }
  })

  fastify.get('/console/logs', async (request, reply) => {
    return { logs: logview.list() }
  })

  fastify.get('/console/log', async (request, reply) => {
    const name = request.query.name
    if (!name) {
      reply.code(400)
      return { error: 'name is required' }
    }
    try {
      return { name, content: logview.read(name) }
    } catch {
      reply.code(404)
      return { error: 'not found' }
    }
  })

  await fastify.listen({ host: '0.0.0.0', port: 3000 })
}

start()
