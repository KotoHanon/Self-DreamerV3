import torch
import torch.nn as nn
import random
from utils import build_bins, symlog, symexp

class RSSM(nn.Module):
    '''Recurretn State Space Model'''
    '''
    x_t : obs_dim
    h_t : state_dim
    z_t : latent_dim
    a_t : action_dim
    '''
    def __init__(self, obs_dim, state_dim, latent_dim, action_dim):
        super().__init__()
        self.state_dim = state_dim
        self.latent_dim = latent_dim
        self.gru = nn.GRU(input_size=latent_dim+action_dim, hidden_size=state_dim, batch_first=True) # [bsz, seq, feat]

        '''Encode the observation (x -> latent)'''
        self.encdoer = nn.Linear(obs_dim, latent_dim)
        self.decoder = nn.Linear(state_dim+latent_dim, obs_dim)
        self.reward_head = nn.Linear(state_dim+latent_dim, 1)
        self.continue_head = nn.Linear(state_dim+latent_dim, 1)

    def forward(self, obs, actions, hidden=None):
        '''
        obs_shape -> [bsz, T, dim]
        hidden is the first hidden state a.k.a h_0
        '''
        # 1. get the latent state z_t
        obs_latent = self.encdoer(obs)

        # 2. send the latent state to sequence model
        concat = torch.cat([obs_latent, actions], dim=-1)
        output, h_n = self.gru(concat, hidden)

        # 3. get the reward and continue
        model_state = torch.cat([obs_latent, output], dim=-1)
        r = self.reward_head(model_state)
        c = self.continue_head(model_state)

        # 4. reconstruct
        rec = self.decoder(model_state)

        return r, c, rec, h_n

class Actor(nn.Module):
    '''
    model_state -> action_dist
    '''
    def __init__(self, state_dim, action_dim, latent_dim, hidden_dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim+latent_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, action_dim)
        )
    
    def forward(self, model_state):
        logits = self.net(model_state) #[bsz, T, action_dim]
        # sample action
        probs = torch.softmax(logits, dim=-1) # logits_shape : [bsz, T, action_dim]
        dist = torch.distributions.Categorical(probs)
        action = dist.sample()
        return action

class Critic(nn.Module):
    def __init__(self, state_dim, latent_dim, hidden_dim, num_bins):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(state_dim+latent_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, num_bins) # output the logits with num_bins
        )
        self.register_buffer("bins", build_bins(num_bins=num_bins))
    
    def forward(self, model_state):
        logits = self.net(model_state) # logits_shape : [bsz, T, num_bins]
        probs = torch.softmax(logits, dim=-1)
        center = self.bins # [num_bins]
        exp_value = torch.sum(probs * center, dim=-1) # [bsz, T]
        return exp_value

class DreamerV3:
    def __init__(self, obs_dim, state_dim, latent_dim, action_dim, hidden_dim, num_bins, lr, batch_size, horizon):
        self.world_model = RSSM(obs_dim=obs_dim, state_dim=state_dim, latent_dim=latent_dim, action_dim=action_dim)
        self.actor = Actor(state_dim=state_dim, action_dim=action_dim, latent_dim=latent_dim, hidden_dim=hidden_dim)
        self.critic = Critic(state_dim=state_dim, latent_dim=latent_dim, hidden_dim=hidden_dim, num_bins=num_bins)
    
        self.optimizer_wm = torch.optim.AdamW(self.world_model.parameters(), lr=lr)
        self.optimizer_actor = torch.optim.AdamW(self.actor.parameters(), lr=lr)
        self.optimizer_critic = torch.optim.AdamW(self.critic.parameters(), lr=lr)

        self.replay_buffer = deque(maxlen=10000) # replay buffer
        self.batch_size = batch_size
        self.horizon = horizon
        self.action_dim = action_dim
    
    def train_world_model(self, num_iters=1):
        # check the length of replay buffer
        if len(self.replay_buffer) < self.batch_size * self.horizon:
            return

        for _ in range(num_iters):
            # sample randomly from batch
            obses, actions, rewards, next_obses, dones = [], [], [], [], [] # list
            seq_len = self.horizon
            for _b in range(self.batch_size):
                idx = random.randint(0, len(self.replay_buffer) - seq_len)
                traj = ilst(self.replay_buffer)[idx:idx+seq_len]
                o, a, r, next_o, d = *traj
                obses.append(o)
                actions.append(a)
                rewards.append(r)
                next_obses.append(next_o)
                dones.append(d)
            
            # convert to tensors
            obses = torch.tensor(obses, dtype=torch.float32) #[bsz, T, obs_dim]
            actions = torch.nn.functional.one_hot(torch.LongTensor(actions), self.action_dim) #[bsz, T, action_dim]
            rewards = torch.tensor(rewards, dtype=torch.float32).unsqueeze(-1) #[bsz, T, 1]
            next_obses = torch.tensor(next_obses, dtype=torch.float32) #[bsz, T, obs_dim]
            dones = torch.tensor(dones, dtype=torch.float32).unsqueeze(-1) #[bsz, T, 1]\
            
            # forward of world model
            rewards_pred, conts_pred, reconstructs, _ = self.world_model(obses, actions)

            # symlog
            symlog_rewards_pred = symlog(rewards_pred)
            symlog_rewards_gt = symlog(rewards)

            # reconstructions loss
            reconstructs_loss = torch.nn.MSELoss(obses, reconstructs)

            # reward loss
            rewards_loss = torch.nn.MSELoss(symlog_rewards_pred, symlog_rewards_gt)

            # continue loss
            conts_gt = 1.0 - dones
            conts_loss = torch.nn.functional.binary_cross_entropy_with_logits(conts_gt, conts_pred)

            # total loss
            loss = reconstructs_loss + rewards_loss + conts_loss

            self.optimizer_wm.zero_grad()
            loss.backward()
            self.optimizer_wm.step()
        
    def imagine_rollout(self, init_obs, horizon=None):
        '''
        imagine in the latent space:
        1. given the initial state: init_obs(or its latens space)
        2. interact with world model, Actor predicts "rewards", "dones" and recurrences "h_t" and "z_t"
        '''

        if horizon is None:
            horizon = self.horizon # if horizon is None, use the default horizon
        
        batch_size = init_obs.shape[0]
        # encode the init_obs into latent
        init_latent = self.world_model.encdoer(init_obs) # [bsz, latent_dim]
        # init the hidden state
        h_0 = torch.zeros(batch_size, self.state_dim)

        # record the imformation
        model_states = []
        rewards = []
        dones = []

        z_t = init_latent
        h_t = h_0

        for t in range(horizon):
            model_state = torch.cat([h_t, z_t], dim=-1)
            actions = self.actor(model_state)
            actions_onehot = torch.nn.functional.one_hot(actions, self.action_dim).to(float)

            # mock
            obs_in = init_obs

            reward_pred, cont_pred, reconstruct, h_next = self.world_model(
                obs_in, actions, hidden=h_t
            )

            h_t = h_next # update the hidden_state
            z_t_next = self.world_model.encdoer(reconstruct)

            # record rewards/dones/model states
            symlog_reward_pred = symlog(reward_pred)
            reward = symexp(symlog_rewards_pred)
            rewards.append(reward)

            cont = torch.sigmoid(cont_pred)
            done = (cont < 0.5).float()
            dones.append(done)

            model_states.append(model_state)

            z_t = z_t_next # update the latent state
        
        # concat
        model_states = torch.stack(model_states, dim=1) # [bsz, T, model_state_dim]
        rewards = torch.stack(rewards, dim=1) # [bsz, T, 1]
        dones = torch.stack(dones, dim=1) # [bsz, T, 1]

        return model_states, rewards, dones




        












# test func
def mock():
    obs_dim = 128
    state_dim = 64
    action_dim = 4
    latent_dim = 32
    batch_size = 16
    T = 10
    obs = torch.randn(batch_size, T, obs_dim)
    action = torch.randn(batch_size, T, action_dim)

    # test RSSM
    rssm = RSSM(obs_dim, state_dim, latent_dim, action_dim)
    r, c, rec, h_n = rssm(obs, action)
    print("Test RSSM: ", r.shape)

    # test actor
    actor = Actor(state_dim, action_dim, latent_dim, 64)
    model_state = torch.randn(batch_size, T, state_dim+latent_dim)
    print("Test Actor: ", actor(model_state).shape)

    # test critic
    critic = Critic(state_dim, latent_dim, 64, 256)
    model_state = torch.randn(batch_size, T, state_dim+latent_dim)
    print("Test Critic: ", critic(model_state).shape)


if __name__ ==  "__main__":
    mock()

