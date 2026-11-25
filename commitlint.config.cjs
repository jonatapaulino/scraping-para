// Mensagens de commit no padrao Conventional Commits — e delas que sai a versao:
//   fix: ...   -> 1.0.1 (correcao)      feat: ...  -> 1.1.0 (novidade)
//   feat!: ... ou "BREAKING CHANGE:" no corpo -> 2.0.0 (quebra compatibilidade)
//   docs/test/ci/build/chore/style -> nao gera versao
// O texto pode (e deve) ser em portugues: "fix(fila): mantem o periodo ao trocar de dataset"
module.exports = {
  extends: ["@commitlint/config-conventional"],
  rules: {
    "subject-case": [0],          // portugues: maiuscula ou nao, tanto faz
    "header-max-length": [2, "always", 100],
    "body-max-line-length": [0],  // corpo livre (listas, links)
    "footer-max-line-length": [0],
  },
};
