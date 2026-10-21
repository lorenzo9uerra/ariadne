const { readFileSync, readdirSync } = require('fs')
const path = require('path')

const LOG_DIR = '/app/logs'

function list() {
  return readdirSync(LOG_DIR)
}

function read(name) {
  const file = path.join(LOG_DIR, name)
  return readFileSync(file, 'utf8')
}

module.exports = { list, read }
