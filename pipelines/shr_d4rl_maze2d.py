
# simple hierarchical diffusers
# shd

import os

import d4rl
import gym
import hydra
import numpy as np
import torch
from tqdm import tqdm
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader

from cleandiffuser.classifier import CumRewClassifier
from cleandiffuser.dataset.d4rl_maze2d_dataset import D4RLMaze2DDataset
from cleandiffuser.dataset.dataset_utils import loop_dataloader
from cleandiffuser.diffusion import DiscreteDiffusionSDE
from cleandiffuser.nn_classifier import HalfJannerUNet1d
from cleandiffuser.nn_diffusion import JannerUNet1d
from cleandiffuser.utils import report_parameters
from utils import set_seed

@hydra.main(config_path="../configs/shd/maze2d", config_name="maze2d", version_base=None)
def pipeline(args):

    set_seed(args.seed)

    save_path = f'results/{args.pipeline_name}/{args.task.env_name}/'
    if os.path.exists(save_path) is False:
        os.makedirs(save_path)

    # ---------------------- Create Dataset ----------------------
    env = gym.make(args.task.env_name)

    # HL -- downsampled
    hl_dataset = D4RLMaze2DDataset( # change the dataset
        env.get_dataset(), horizon=args.task.horizon, discount=args.discount,
        noreaching_penalty=args.noreaching_penalty,)
    hl_dataloader = DataLoader(
        hl_dataset, batch_size=args.batch_size, shuffle=True, num_workers=4, pin_memory=True, drop_last=True)
    hl_obs_dim, hl_act_dim = hl_dataset.o_dim, hl_dataset.a_dim

    # LL -- short horizon
    ll_dataset = D4RLMaze2DDataset(
        env.get_dataset(), horizon=args.task.ll_horizon, discount=args.discount,
        noreaching_penalty=args.noreaching_penalty,)
    ll_dataloader = DataLoader(
        ll_dataset, batch_size=args.batch_size, shuffle=True, num_workers=4, pin_memory=True, drop_last=True)
    ll_obs_dim, ll_act_dim = ll_dataset.o_dim, ll_dataset.a_dim

    # --------------- Network Architecture -----------------
    # HL 
    hl_nn_diffusion = JannerUNet1d(
        hl_obs_dim + hl_act_dim, model_dim=args.model_dim, emb_dim=args.model_dim, dim_mult=args.task.dim_mult,
        timestep_emb_type="positional", attention=False, kernel_size=5)
    
    hl_nn_classifier = HalfJannerUNet1d(
        args.task.hl_horizon, hl_obs_dim + hl_act_dim, out_dim=1,
        model_dim=args.model_dim, emb_dim=args.model_dim, dim_mult=args.task.dim_mult,
        timestep_emb_type="positional", kernel_size=3)
    
    # LL
    ll_nn_diffusion = JannerUNet1d(
        ll_obs_dim + ll_act_dim, model_dim=args.model_dim, emb_dim=args.model_dim, dim_mult=args.task.dim_mult,
        timestep_emb_type="positional", attention=False, kernel_size=5)

    ll_nn_classifier = HalfJannerUNet1d(
        args.task.ll_horizon, ll_obs_dim + ll_act_dim, out_dim=1,
        model_dim=args.model_dim, emb_dim=args.model_dim, dim_mult=args.task.dim_mult,
        timestep_emb_type="positional", kernel_size=3)
    
    print(f"======================= Parameter Report of Diffusion Model =======================")
    report_parameters(hl_nn_diffusion)
    report_parameters(ll_nn_diffusion)

    print(f"======================= Parameter Report of Classifier =======================")
    report_parameters(hl_nn_classifier)
    report_parameters(ll_nn_classifier)

    print(f"==============================================================================")

    # --------------- Classifier Guidance --------------------
    hl_classifier = CumRewClassifier(hl_nn_classifier, device=args.device)
    ll_classifier = CumRewClassifier(ll_nn_classifier, device=args.device)

    # ----------------- Masking -------------------
    hl_fix_mask = torch.zeros((args.task.hl_horizon, hl_obs_dim + hl_act_dim))
    hl_fix_mask[0, :hl_obs_dim] = 1.
    hl_loss_weight = torch.ones((args.task.hl_horizon, hl_obs_dim + hl_act_dim))
    hl_loss_weight[0, hl_obs_dim:] = args.action_loss_weight

    # !! Low-level is goal conditioned !!
    ll_fix_mask = torch.zeros((args.task.ll_horizon, ll_obs_dim + ll_act_dim))
    ll_fix_mask[0, :ll_obs_dim] = 1.
    ll_fix_mask[-1, :ll_obs_dim] = 1.
    ll_loss_weight = torch.ones((args.task.ll_horizon, ll_obs_dim + ll_act_dim))
    ll_loss_weight[0, ll_obs_dim:] = args.action_loss_weight

    # --------------- Diffusion Model --------------------
    hl_agent = DiscreteDiffusionSDE(
        hl_nn_diffusion, None,
        fix_mask=hl_fix_mask, loss_weight=hl_loss_weight, classifier=hl_classifier, ema_rate=args.ema_rate,
        device=args.device, diffusion_steps=args.diffusion_steps, predict_noise=args.predict_noise)
    
    ll_agent = DiscreteDiffusionSDE(
        ll_nn_diffusion, None,
        fix_mask=ll_fix_mask, loss_weight=ll_loss_weight, classifier=ll_classifier, ema_rate=args.ema_rate,
        device=args.device, diffusion_steps=args.diffusion_steps, predict_noise=args.predict_noise)
    
    # ---------------------- Training ----------------------
    if args.mode == "train":

        hl_diffusion_lr_scheduler = CosineAnnealingLR(hl_agent.optimizer, args.diffusion_gradient_steps)
        ll_diffusion_lr_scheduler = CosineAnnealingLR(ll_agent.optimizer, args.diffusion_gradient_steps)

        hl_classifier_lr_scheduler = CosineAnnealingLR(hl_agent.classifier.optim, args.classifier_gradient_steps)
        ll_classifier_lr_scheduler = CosineAnnealingLR(ll_agent.classifier.optim, args.classifier_gradient_steps)

        hl_agent.train()
        ll_agent.train()

        hl_n_gradient_step = 0
        ll_n_gradient_step = 0

        hl_log = {"hl_avg_loss_diffusion": 0., "hl_avg_loss_classifier": 0.}
        ll_log = {"ll_avg_loss_diffusion": 0., "ll_avg_loss_classifier": 0.}

        pbar = tqdm(total=args.diffusion_gradient_steps)

        for hl_batch, ll_batch in zip(loop_dataloader(hl_dataloader), loop_dataloader(ll_dataloader)):
            
            # hl downsample by the ll_horizon, namely horizon / ll_horizon
            hl_obs = hl_batch["obs"]["state"][:, ::args.task.ll_horizon, :].to(args.device)
            hl_act = hl_batch["act"][:,::args.task.ll_horizon, :].to(args.device)
            hl_val = hl_batch["val"].to(args.device)
            hl_x = torch.cat([hl_obs, hl_act], -1)

            ll_obs = ll_batch["obs"]["state"].to(args.device)
            ll_act = ll_batch["act"].to(args.device)
            ll_val = ll_batch["val"].to(args.device)
            ll_x = torch.cat([ll_obs, ll_act], -1)

            # ----------- Gradient Step ------------
            hl_log["hl_avg_loss_diffusion"] += hl_agent.update(hl_x)['loss']
            ll_log["ll_avg_loss_diffusion"] += ll_agent.update(ll_x)['loss']

            hl_diffusion_lr_scheduler.step()
            ll_diffusion_lr_scheduler.step()

            if hl_n_gradient_step <= args.classifier_gradient_steps:
                hl_log["hl_avg_loss_classifier"] += hl_agent.update_classifier(hl_x, hl_val)['loss']
                hl_classifier_lr_scheduler.step()

            if ll_n_gradient_step <= args.classifier_gradient_steps:
                ll_log["ll_avg_loss_classifier"] += ll_agent.update_classifier(ll_x, ll_val)['loss']
                ll_classifier_lr_scheduler.step()

            # ----------- Logging ------------
            if (hl_n_gradient_step + 1) % args.log_interval == 0:
                hl_log["hl_gradient_steps"] = hl_n_gradient_step + 1
                hl_log["hl_avg_loss_diffusion"] /= args.log_interval
                hl_log["hl_avg_loss_classifier"] /= args.log_interval
                print(hl_log)
                hl_log = {"hl_avg_loss_diffusion": 0., "hl_avg_loss_classifier": 0.}

            if (ll_n_gradient_step + 1) % args.log_interval == 0:
                ll_log["ll_gradient_steps"] = ll_n_gradient_step + 1
                ll_log["ll_avg_loss_diffusion"] /= args.log_interval
                ll_log["ll_avg_loss_classifier"] /= args.log_interval
                print(ll_log)
                ll_log = {"ll_avg_loss_diffusion": 0., "ll_avg_loss_classifier": 0.}

            # ----------- Saving ------------
            if (hl_n_gradient_step + 1) % args.save_interval == 0:
                hl_agent.save(save_path + f"hl_diffusion_ckpt_{hl_n_gradient_step + 1}.pt")
                hl_agent.classifier.save(save_path + f"hl_classifier_ckpt_{hl_n_gradient_step + 1}.pt")
                hl_agent.save(save_path + f"hl_diffusion_ckpt_latest.pt")
                hl_agent.classifier.save(save_path + f"hl_classifier_ckpt_latest.pt")

                # print('save to', save_path + f"hl_diffusion_ckpt_{hl_n_gradient_step + 1}.pt")

            if (ll_n_gradient_step + 1) % args.save_interval == 0:
                ll_agent.save(save_path + f"ll_diffusion_ckpt_{ll_n_gradient_step + 1}.pt")
                ll_agent.classifier.save(save_path + f"ll_classifier_ckpt_{ll_n_gradient_step + 1}.pt")
                ll_agent.save(save_path + f"ll_diffusion_ckpt_latest.pt")
                ll_agent.classifier.save(save_path + f"ll_classifier_ckpt_latest.pt")

            hl_n_gradient_step += 1
            ll_n_gradient_step += 1

            if hl_n_gradient_step >= args.diffusion_gradient_steps:
                break

            if ll_n_gradient_step >= args.classifier_gradient_steps:
                break

            pbar.update(1)

        pbar.close()
    # ---------------------- Inference ----------------------
    elif args.mode == "inference":

        hl_agent.load(save_path + f"hl_diffusion_ckpt_{args.ckpt}.pt")
        hl_agent.classifier.load(save_path + f"hl_classifier_ckpt_{args.ckpt}.pt")

        ll_agent.load(save_path + f"ll_diffusion_ckpt_{args.ckpt}.pt")
        ll_agent.classifier.load(save_path + f"ll_classifier_ckpt_{args.ckpt}.pt")

        hl_agent.eval()
        ll_agent.eval()

        env_eval = gym.vector.make(args.task.env_name, args.num_envs)
        normalizer = hl_dataset.get_normalizer()
        episode_rewards = []

        hl_prior = torch.zeros((args.num_envs, args.task.hl_horizon, hl_obs_dim + hl_act_dim), device=args.device)
        ll_prior = torch.zeros((args.num_envs, args.task.ll_horizon, ll_obs_dim + ll_act_dim), device=args.device)

        for _ in range(args.num_episodes):

            obs, ep_reward, cum_done, t = env_eval.reset(), 0., 0., 0

            while not np.all(cum_done) and t < 1000 + 1:
                # normalize obs
                obs = torch.tensor(normalizer.normalize(obs), device=args.device, dtype=torch.float32)

                # HL
                hl_prior[:, 0, :hl_obs_dim] = obs
                hl_traj, hl_log = hl_agent.sample(
                    hl_prior.repeat(args.num_candidates, 1, 1),
                    solver=args.solver,
                    n_samples=args.num_candidates * args.num_envs,
                    sample_steps=args.sampling_steps,
                    use_ema=args.use_ema, w_cg=args.task.w_cg, temperature=args.temperature)

                # select the best plan
                hl_logp = hl_log["log_p"].view(args.num_candidates, args.num_envs, -1).sum(-1)
                hl_idx = hl_logp.argmax(0)
                # get next obs
                hl_next_state = hl_traj.view(args.num_candidates, args.num_envs, args.task.hl_horizon, -1)[
                      hl_idx, torch.arange(args.num_envs), 1, :hl_obs_dim]

                # LL
                ll_prior[:, 0, :ll_obs_dim] = obs
                # !! Low-level is goal conditioned !!
                ll_prior[:, -1, :ll_obs_dim] = hl_next_state
                ll_traj, ll_log = ll_agent.sample(
                    ll_prior.repeat(args.num_candidates, 1, 1),
                    solver=args.solver,
                    n_samples=args.num_candidates * args.num_envs,
                    sample_steps=args.sampling_steps,
                    use_ema=args.use_ema, w_cg=args.task.w_cg, temperature=args.temperature)
                
                # select the best plan
                ll_logp = ll_log["log_p"].view(args.num_candidates, args.num_envs, -1).sum(-1)
                ll_idx = ll_logp.argmax(0)
                act = ll_traj.view(args.num_candidates, args.num_envs, args.task.ll_horizon, -1)[
                    ll_idx, torch.arange(args.num_envs), 0, ll_obs_dim:]
                act = act.clip(-1., 1.).cpu().numpy()
                
                # step
                obs, rew, done, info = env_eval.step(act)

                t += 1
                cum_done = done if cum_done is None else np.logical_or(cum_done, done)
                ep_reward += rew
                print(f'[t={t}] xy: {obs[:, :2]}')
                print(f'[t={t}] cum_rew: {ep_reward}, '
                      f'logp: {hl_logp[hl_idx, torch.arange(args.num_envs)]}')

            # clip the reward to [0, 1] since the max cumulative reward is 1
            episode_rewards.append(np.clip(ep_reward, 0., 1.))

        episode_rewards = [list(map(lambda x: env.get_normalized_score(x), r)) for r in episode_rewards]
        episode_rewards = np.array(episode_rewards)
        print(np.mean(episode_rewards, -1), np.std(episode_rewards, -1))

    else:
        raise ValueError(f"Invalid mode: {args.mode}")


if __name__ == "__main__":
    pipeline()