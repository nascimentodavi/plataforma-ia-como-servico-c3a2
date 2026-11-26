"""Carga de trabalho de IA: classificador de sentimento + gerador de embeddings.

A IA e uma caixa-preta para a plataforma (a sofisticacao do modelo nao e o foco):
- Sentimento: TF-IDF + Regressao Logistica (scikit-learn), treinado no build da imagem.
- Embeddings: HashingVectorizer de n-gramas de caracteres, normalizado (L2).
  E deterministico, nao precisa de download nem de GPU, e funciona 100% offline.
"""
import os

import joblib
from sklearn.feature_extraction.text import HashingVectorizer, TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline

VERSAO = "sentimento-tfidf-lr-v1"
DIMENSAO = int(os.getenv("DIMENSAO_VETOR", "384"))
CAMINHO_MODELO = os.getenv("CAMINHO_MODELO", "modelo.joblib")

POSITIVOS = [
    "servico excelente, recomendo a todos", "adorei o produto, chegou rapido",
    "atendimento otimo e muito educado", "muito bom, superou minhas expectativas",
    "qualidade incrivel pelo preco", "entrega no prazo e bem embalado",
    "experiencia maravilhosa, voltarei a comprar", "funciona perfeitamente",
    "equipe atenciosa resolveu meu problema", "gostei bastante, nota dez",
    "o aplicativo e rapido e facil de usar", "produto de otima qualidade",
    "fiquei muito satisfeito com a compra", "recomendo, vale cada centavo",
    "suporte eficiente e cordial", "melhor compra que fiz este ano",
    "tudo certo, parabens pelo trabalho", "atendimento rapido e eficaz",
    "a comida estava deliciosa", "ambiente agradavel e limpo",
    "o curso e excelente e bem explicado", "professor muito didatico",
    "sistema estavel e confiavel", "interface bonita e intuitiva",
    "preco justo e entrega rapida", "amei, perfeito",
]
NEGATIVOS = [
    "servico pessimo, nao recomendo", "o produto chegou quebrado",
    "atendimento horrivel e grosseiro", "muito ruim, uma decepcao",
    "qualidade terrivel, nao vale o preco", "a entrega atrasou duas semanas",
    "experiencia horrorosa, nunca mais compro", "nao funciona, parou no primeiro dia",
    "ninguem resolveu meu problema", "detestei, nota zero",
    "o aplicativo trava o tempo todo", "produto de pessima qualidade",
    "fiquei muito insatisfeito com a compra", "dinheiro jogado fora",
    "suporte demorado e sem educacao", "pior compra que ja fiz",
    "tudo errado, um absurdo", "atendimento lento e ineficiente",
    "a comida estava fria e sem gosto", "ambiente sujo e barulhento",
    "o curso e confuso e mal explicado", "professor desorganizado",
    "sistema instavel e cheio de erros", "interface feia e confusa",
    "caro demais e entrega lenta", "odiei, horrivel",
]


def treinar(caminho: str = CAMINHO_MODELO):
    textos = POSITIVOS + NEGATIVOS
    rotulos = ["positivo"] * len(POSITIVOS) + ["negativo"] * len(NEGATIVOS)
    modelo = make_pipeline(
        TfidfVectorizer(strip_accents="unicode", lowercase=True, ngram_range=(1, 2),
                        analyzer="word", sublinear_tf=True),
        LogisticRegression(max_iter=1000, C=5.0),
    )
    modelo.fit(textos, rotulos)
    joblib.dump(modelo, caminho)
    return modelo


def carregar(caminho: str = CAMINHO_MODELO):
    if os.path.exists(caminho):
        return joblib.load(caminho)
    return treinar(caminho)


_vetorizador = HashingVectorizer(
    n_features=DIMENSAO, analyzer="char_wb", ngram_range=(3, 5), alternate_sign=False,
    norm="l2", lowercase=True, strip_accents="unicode",
)


def classificar(modelo, texto: str) -> dict:
    probabilidades = modelo.predict_proba([texto])[0]
    indice = int(probabilidades.argmax())
    return {"sentimento": str(modelo.classes_[indice]),
            "confianca": round(float(probabilidades[indice]), 4)}


def embedding(texto: str) -> list:
    return _vetorizador.transform([texto]).toarray()[0].round(6).tolist()
