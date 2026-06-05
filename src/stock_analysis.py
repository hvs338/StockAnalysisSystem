import boto3

import src.schwab_client as schwab_client


class BedrockClient:
    def __init__(self, client):
        self.client = client
        self.model = "anthropic.claude-opus-4-7"

    def generate_content(self, model_id, input_text):
        response = self.client.generate_content(
            modelId=model_id,
            inputContent=[{"role": "user", "input": input_text}]
        )
        return response['content']
    

    def stock_analysis(self, stock_symbol):
        prompt = f"""
        Provide a detailed analysis of the stock {stock_symbol}, including recent performance, key financial metrics, and future outlook.
        """
        analysis = self.generate_content(model_id="amazon.titan-20240926", input_text=prompt)
        return analysis