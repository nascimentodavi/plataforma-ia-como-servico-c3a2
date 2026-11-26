"""Treina o modelo no build da imagem (RUN python treinar.py)."""
import modelo

if __name__ == "__main__":
    m = modelo.treinar()
    print("modelo treinado:", modelo.VERSAO, "->", modelo.CAMINHO_MODELO)
    print("teste:", modelo.classificar(m, "servico excelente"))
