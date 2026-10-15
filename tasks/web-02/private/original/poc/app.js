const fastify = require('fastify')({ logger: true });

async function start() {
  // Register fastify-express for Express-style middleware support
  await fastify.register(require('@fastify/express'));

  // Middleware to block /admin route
  fastify.use('/admin', (req, res, next) => {
    res.statusCode = 403;
    res.end('Forbidden: Access to /admin is blocked');
  });

  // Sample routes
  fastify.get('/', async (request, reply) => {
    return { message: 'Welcome to the homepage' };
  });

  fastify.get('/admin', async (request, reply) => {
    return { message: 'Admin panel' };
  });

  fastify.get('/admin/dashboard', async (request, reply) => {
    return { message: 'Admin dashboard' };
  });

  // Start server
  try {
    await fastify.listen({ port: 3000 });
  } catch (err) {
    fastify.log.error(err);
    process.exit(1);
  }
}

start();
